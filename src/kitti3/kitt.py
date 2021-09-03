import enum
import functools
import logging
import time
from typing import List, Optional

import i3ipc

from .util import Client, Position, Rect, Shape


class Event(enum.Enum):
    KEYBIND = enum.auto()
    SPAWNED = enum.auto()
    FLOATED = enum.auto()
    MOVED = enum.auto()


class Kitt:
    def __init__(
        self,
        conn: i3ipc.Connection,
        name: str,
        shape: Shape,
        pos: Position,
        client: Client,
        client_argv: Optional[List[str]] = None,
    ):
        self.i3 = conn
        self.name = name
        self.shape = shape
        self.pos = pos
        self.client = client
        self.client_argv = client_argv

        self._log = logging.getLogger(self.__class__.__name__)
        self.con_id: Optional[int] = None
        self.con_ws: Optional[i3ipc.WorkspaceReply] = None
        self.focused_ws: Optional[i3ipc.WorkspaceReply] = None

        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("window::floating", self.on_floated)
        self.i3.on("window::move", self.on_moved)
        self.i3.on("shutdown::exit", self.on_shutdown)

    def loop(self) -> None:
        """Enter listening mode, awaiting IPC events."""
        try:
            self.i3.main()
        finally:
            self.i3.main_quit()

    def on_keybind(self, _, be: i3ipc.BindingEvent) -> None:
        """Toggle the visibility of the client window when the appropriate keybind
        command is triggered by the user.

        Hide the client window if it is present on the focused workspace, otherwise
        fetch it from its current workspace (scratchpad or regular). Spawn a new client
        instance if one does not presently exist.
        """
        if be.binding.command != f"nop {self.name}":
            return
        self._log.debug("%s", be.binding.command)
        self._refresh()
        if self.con_id is None:
            self.spawn()
        elif self.con_ws.name == self.focused_ws.name:
            cmd = f"[con_id={self.con_id}] floating enable, move scratchpad"
            self._log.debug("%s", cmd)
            self.i3.command(cmd)
        else:
            self.align_to_ws(Event.KEYBIND)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Float the client window once it has settled after spawning.

        The act of floating will trigger `on_floated()`, which will take care of
        alignment.
        """
        con = we.container
        if getattr(con, self.client.cattr.value) != self.name:
            return
        self._log.debug("matched %s", con.id)
        self.con_id = con.id
        self._refresh()
        self.align_to_ws(Event.SPAWNED)

    def on_floated(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the client window is aligned to its workspace when transitioning
        from tiled to floated.
        """
        con = we.container
        if not (
            # cf on_moved, for i3 con is our target, but .type == "floating_con" is only
            # used for the floating wrapper. Hence the need to check .floating.
            (con.type == "floating_con" or con.floating == "user_on")
            and con.id == self.con_id
        ):
            return
        self._refresh()
        if self.con_ws is None:
            self._log.warning("despawn guard tripped: client workspace is None")
            return
        # toggle-while-tiled trigger repression
        elif self.con_ws.name == "__i3_scratch":
            return
        self.align_to_ws(Event.FLOATED)

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the client window is positioned and resized when moved to a
        different sized workspace (e.g. on a different monitor).

        If the client has been manually tiled by the user it will not be re-floated.
        """
        con = we.container
        if not (
            # ignore tiled cons
            con.type == "floating_con"
            # event's con is floating wrapper for i3, but target con for sway
            and (con.id == self.con_id or con.find_by_id(self.con_id))
        ):
            return
        self._refresh()
        if self.con_ws is None:
            self._log.warning("despawn guard tripped: client workspace is None")
            return
        elif (
            # avoid double-triggering
            self.con_ws.name == getattr(self.focused_ws, "name", "")
            # avoid triggering on a move to the scratchpad
            or self.con_ws.name == "__i3_scratch"
        ):
            return
        self.align_to_ws(Event.MOVED)

    def on_shutdown(self, _, se: i3ipc.ShutdownEvent):
        self._log.debug("received IPC shutdown command; exiting...")
        exit(0)

    def spawn(self) -> None:
        """Spawn a new client window associated with the name of this Kitti3 instance."""
        if self.client.cmd is None:
            self._log.warning("unable to comply; spawning is disabled")
            return
        cmd = f"exec {self.client.cmd.format(self._escape(self.name))}"
        if self.client_argv:
            cmd = f"{cmd} {' '.join(self.client_argv)}"
        self._log.debug("%s", cmd)
        self.i3.command(cmd)

    @functools.lru_cache()
    def con_rect(self, abs_ref: Rect = None) -> Rect:
        # relative/ppt
        if abs_ref is None:
            width = round(self.shape.x * 100)
            height = round(self.shape.y * 100)
            x = {
                "L": 0,
                "C": round(50 - (width / 2)),
                "R": 100 - width,
            }[self.pos.x]
            y = {
                "T": 0,
                "C": round(50 - (height / 2)),
                "B": 100 - height,
            }[self.pos.y]
        # absolute/px
        else:
            width = round(abs_ref.w * self.shape.x)
            height = round(abs_ref.h * self.shape.y)
            x = {
                "L": abs_ref.x,
                "C": abs_ref.x + round((abs_ref.w / 2) - (width / 2)),
                "R": abs_ref.x + abs_ref.w - width,
            }[self.pos.x]
            y = {
                "T": abs_ref.y,
                "C": abs_ref.y + round((abs_ref.h / 2) - (height / 2)),
                "B": abs_ref.y + abs_ref.h - height,
            }[self.pos.y]
        return Rect(x, y, width, height)

    @staticmethod
    def _escape(arg: str) -> str:
        if " " in arg:
            arg = f'"{arg}"'
        return arg

    def _refresh(self) -> None:
        """Update the information on the presence of the associated client instance,
        its workspace and the focused workspace.
        """
        tree = self.i3.get_tree()
        # TODO con_mark: or (self.client.cattr is con_mark and self.no_lock)
        if self.con_id is None:
            for con in tree:
                # TODO con_mark: self.name in ...
                if getattr(con, self.client.cattr.value) == self.name:
                    self.con_id = con.id
                    self.con_ws = con.workspace()
                    break
            else:
                self.con_ws = None
        else:
            try:
                self.con_ws = tree.find_by_id(self.con_id).workspace()
            # The client instance has despawned since the last refresh
            except AttributeError:
                self.con_id = None
                self.con_ws = None
        # WS refs from get_tree() are stubs with no focus info, so have to perform a
        # second query
        for ws in self.i3.get_workspaces():
            if ws.focused:
                self.focused_ws = ws
                break
        else:
            self.focused_ws = None

        self._log.debug(
            "con_id: %s, con_ws: %s, focused_ws: %s",
            self.con_id,
            getattr(self.con_ws, "name", None),
            getattr(self.focused_ws, "name", None),
        )

    def align_to_ws(self, context: Event) -> None:
        raise NotImplementedError


class Kitts(Kitt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sway_timing_compat = True  # TODO: add as arg

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        # Currently under Sway, if a container on an inactive workspace is moved, it
        # is forcibly reparented to its output's active workspace. Therefore, this
        # feature is diabled, pending https://github.com/swaywm/sway/issues/6465 .
        return

    def spawn(self) -> None:
        r = self.con_rect()
        self.i3.command(
            f"for_window [{self.client.cattr}={self._escape(self.name)}] 'floating"
            f" enable, border none, resize set {r.w}ppt {r.h}ppt, move position"
            f" {r.x}ppt {r.y}ppt'"
        )
        super().spawn()

    def align_to_ws(self, trigger: Event) -> None:
        if trigger == Event.SPAWNED:
            return
        if trigger == Event.FLOATED and self.sway_timing_compat:
            time.sleep(0.02)
        self._log.debug(trigger)
        r = self.con_rect()
        crit = f"[con_id={self.con_id}]"
        resize = f"resize set {r.w}ppt {r.h}ppt"
        move = f"move position {r.x}ppt {r.y}ppt"
        if trigger == Event.KEYBIND:
            fetch = f"move container to workspace {self.focused_ws.name}"
            cmd = f"{crit} {fetch}, {resize}, {move}, focus"
        else:
            cmd = f"{crit} {resize}, {move}"
        self._log.debug(cmd)
        ret = self.i3.command(cmd)
        self._log.debug("%s", [s or e for s, e in [(r.success, r.error) for r in ret]])


class Kitti3(Kitt):
    def align_to_ws(self, context: Event) -> None:
        # Under i3, in multi-output configurations, a ppt move is considered relative
        # to the rect defined by the bounding box of all outputs, not by the con's
        # workspace. Yes, this is madness, and so we have to do absolute moves (and
        # therefore we also do absolute resizes to stay consistent).
        if context == Event.SPAWNED:
            # floating will trigger on_floated to do the actual alignment
            self.i3.command(f"[con_id={self.con_id}] floating enable, border none")
            return
        ws = self.focused_ws if context == Event.KEYBIND else self.con_ws
        r = self.con_rect(Rect.from_i3ipc(ws.rect))
        crit = f"[con_id={self.con_id}]"
        resize = f"resize set {r.w}px {r.h}px"
        move = f"move absolute position {r.x}px {r.y}px"
        if context == Event.KEYBIND:
            fetch = f"move container to workspace {ws.name}"
            cmd = f"{crit} {fetch}, {resize}, {move}, focus"
        else:
            cmd = f"{crit} {resize}, {move}"
        self._log.debug(cmd)
        ret = self.i3.command(cmd)
        self._log.debug("%s", [s or e for s, e in [(r.success, r.error) for r in ret]])
