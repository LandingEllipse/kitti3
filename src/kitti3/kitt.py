import enum
import functools
import logging
import time
from types import SimpleNamespace
from typing import List, Optional

import i3ipc

from .util import AnimParams, Client, Loc, Pos, Rect, Shape, animate


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
        pos: Pos,
        client: Client,
        client_argv: Optional[List[str]],
        anim: AnimParams,
        loyal: bool,
        crosstalk_delay: Optional[float],
    ):
        self.i3 = conn
        self.name = name
        self.shape = shape
        self.pos = pos
        self.client = client
        self.client_argv = client_argv
        self.anim = anim
        self.loyal = loyal
        self.crosstalk_delay = crosstalk_delay

        self.log = logging.getLogger(self.__class__.__name__)
        self.debug = self.log.getEffectiveLevel() == logging.DEBUG
        self.con_id: Optional[int] = None
        self.con_ws: Optional[i3ipc.Con] = None
        self.focused_ws: Optional[i3ipc.Con] = None
        self.commands = SimpleNamespace(
            crit="[{}={}]",
            fetch="move container to workspace {}",
            float_="floating enable, border none",
            focus="focus",
            hide="floating enable, move scratchpad",
            move="move position {}ppt {}ppt",
            move_abs="move absolute position {}px {}px",
            resize="resize set {}ppt {}ppt",
            resize_abs="resize set {}px {}px",
            rule="for_window",
        )

        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("window::floating", self.on_floated)
        self.i3.on("window::move", self.on_moved)
        self.i3.on("shutdown::exit", self.on_shutdown)

    def align_to_ws(self, context: Event) -> None:
        raise NotImplementedError

    def loop(self) -> None:
        """Enter listening mode, awaiting IPC events."""
        try:
            self.i3.main()
        finally:
            self.i3.main_quit()

    def on_keybind(self, _, be: i3ipc.BindingEvent) -> None:
        """Toggle the visibility of the client window when the appropriate keybind
        command is triggered by the user. Attempt to spawn a client if none is found.
        """
        if be.binding.command != f"nop {self.name}":
            return
        self.log.debug("%s", be.binding.command)
        if not self.refresh():
            if self.con_id is None:
                self.spawn()
        elif self.con_ws.name == self.focused_ws.name:
            self.send(self.commands.hide)
        else:
            self.align_to_ws(Event.KEYBIND)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Bind to a client with a criterium attribute matching Kitti3's instance name."""
        con = we.container
        if getattr(con, self.client.cattr.value) != self.name:
            return
        self.log.debug(
            '[%s="%s"] matched on con_id: %s',
            self.client.cattr.value,
            self.name,
            con.id,
        )
        if self.con_id is not None and self.loyal:
            self.log.warning("loyal to %s; ignoring %s", self.con_id, con.id)
            return
        self.con_id = con.id
        if self.refresh():
            self.align_to_ws(Event.SPAWNED)

    def on_floated(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the client window is aligned to its workspace when transitioning
        from tiled to floated.
        """
        con = we.container
        if not (
            con.id == self.con_id
            # cf on_moved, for i3 con is our target, but .type == "floating_con" is only
            # used for the floating wrapper. Hence the need to check .floating.
            and (con.type == "floating_con" or con.floating == "user_on")
        ):
            return
        if not self.refresh():
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
        if not self.refresh():
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
        self.log.debug("received IPC shutdown command; exiting...")
        exit(0)

    def spawn(self) -> None:
        """Spawn a new client window associated with the name of this Kitti3 instance."""
        if self.client.cmd is None:
            self.log.warning("unable to comply: spawning is disabled")
            return
        cmd = f"exec {self.client.cmd.format(self._escape(self.name))}"
        if self.client_argv:
            cmd = f"{cmd} {' '.join(self.client_argv)}"
        reply = self.i3.command(cmd)[0]
        self.log.debug("%s -> %s", cmd, reply.success and "OK" or reply.error)

    def send(self, *cmds: str) -> None:
        c = self.commands
        crit = c.crit.format("con_id", self.con_id)
        payload = f"{crit} {', '.join(cmds)}"
        replies = self.i3.command(payload)
        if self.debug:
            self.log.debug(crit)
            for cmd, reply in zip(cmds, replies):
                self.log.debug("  %s -> %s", cmd, reply.success and "OK" or reply.error)

    def send_rule(self, *cmds: str) -> None:
        c = self.commands
        crit = c.crit.format(self.client.cattr, self._escape(self.name))
        pre = f"{c.rule} {crit}"
        cmd_str = ", ".join(cmds)
        payload = f"{pre} '{cmd_str}'"
        reply = self.i3.command(payload)[0]
        if self.debug:
            self.log.debug(pre)
            self.log.debug(
                "  '%s' -> %s", cmd_str, reply.success and "OK" or reply.error
            )

    def refresh(self) -> bool:
        """Update the information on the presence of the associated client instance,
        its workspace and the focused workspace.
        """
        tree = self.i3.get_tree()
        # TODO con_mark: or (self.client.cattr is con_mark and not self.loyal)
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
            # the client instance has despawned since the last refresh
            except AttributeError:
                self.con_id = None
                self.con_ws = None
        # WS refs from get_tree() are stubs with no focus info,
        # so have to perform a second query
        for ws in self.i3.get_workspaces():
            if ws.focused:
                self.focused_ws = ws
                break
        else:
            self.focused_ws = None

        self.log.debug(
            "con_id: %s, con_ws: %s, focused_ws: %s",
            self.con_id,
            getattr(self.con_ws, "name", None),
            getattr(self.focused_ws, "name", None),
        )
        ok = None not in (self.con_id, self.con_ws, self.focused_ws)
        if not ok:
            if self.con_id is None:
                self.log.info('no con matching [%s="%s"]', self.client.cattr, self.name)
            else:
                self.log.warning("missing workspace guard tripped")
        return ok

    @functools.lru_cache()
    def con_rect(self, abs_ref: Rect = None) -> Rect:
        # relative/ppt
        if abs_ref is None:
            width = round(self.shape.x * 100)
            height = round(self.shape.y * 100)
            x = {
                Loc.LEFT: 0,
                Loc.CENTER: round(50 - (width / 2)),
                Loc.RIGHT: 100 - width,
            }[self.pos.x]
            y = {
                Loc.TOP: 0,
                Loc.CENTER: round(50 - (height / 2)),
                Loc.BOTTOM: 100 - height,
            }[self.pos.y]
        # absolute/px
        else:
            width = round(abs_ref.w * self.shape.x)
            height = round(abs_ref.h * self.shape.y)
            x = {
                Loc.LEFT: abs_ref.x,
                Loc.CENTER: abs_ref.x + round((abs_ref.w / 2) - (width / 2)),
                Loc.RIGHT: abs_ref.x + abs_ref.w - width,
            }[self.pos.x]
            y = {
                Loc.TOP: abs_ref.y,
                Loc.CENTER: abs_ref.y + round((abs_ref.h / 2) - (height / 2)),
                Loc.BOTTOM: abs_ref.y + abs_ref.h - height,
            }[self.pos.y]
        return Rect(x, y, width, height)

    @staticmethod
    def _escape(arg: str) -> str:
        if " " in arg:
            arg = f'"{arg}"'
        return arg


class Kitts(Kitt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        # Currently under Sway, if a container on an inactive workspace is moved, it
        # is forcibly reparented to its output's active workspace. Therefore, this
        # feature is diabled, pending https://github.com/swaywm/sway/issues/6465 .
        return

    def spawn(self) -> None:
        if self.client.cmd is None:
            return
        r = self.con_rect()
        c = self.commands
        self.send_rule(c.float_, c.resize.format(r.w, r.h), c.move.format(r.x, r.y))
        super().spawn()

    def align_to_ws(self, trigger: Event) -> None:
        if trigger == Event.SPAWNED:
            return
        if trigger == Event.FLOATED and self.crosstalk_delay is not None:
            time.sleep(self.crosstalk_delay)
        self.log.debug(trigger)
        r = self.con_rect()
        c = self.commands
        if trigger == Event.KEYBIND:
            if self.anim.enabled and self.anim.anchor is not None:
                role_x, role_y, start, end = {
                    Loc.LEFT: ("{}", r.y, 0 - r.w, r.x),
                    Loc.RIGHT: ("{}", r.y, 100, r.x),
                    Loc.TOP: (r.x, "{}", 0 - r.h, r.y),
                    Loc.BOTTOM: (r.x, "{}", 100, r.y),
                }[self.anim.anchor]
                move_partial = c.move.format(role_x, role_y)

                def move_cb(frame: int, pos: int) -> None:
                    # ensure first frame move lands in same transaction as fetch
                    if frame == 0:
                        self.send(
                            c.fetch.format(self.focused_ws.name),
                            c.resize.format(r.w, r.h),
                            move_partial.format(pos),
                            c.focus,
                        )
                    else:
                        self.send(move_partial.format(pos))

                animate(move_cb, start, end, self.anim.enter_dur, self.anim.fps)
            else:
                self.send(
                    c.fetch.format(self.focused_ws.name),
                    c.resize.format(r.w, r.h),
                    c.move.format(r.x, r.y),
                    c.focus,
                )
        else:
            self.send(c.resize.format(r.w, r.h), c.move.format(r.x, r.y))


class Kitti3(Kitt):
    def align_to_ws(self, context: Event) -> None:
        # Under i3, in multi-output configurations, a ppt move is considered relative
        # to the rect defined by the bounding box of all outputs, not by the con's
        # workspace. Yes, this is madness, and so we have to do absolute moves (and
        # therefore we also do absolute resizes to stay consistent).
        c = self.commands
        if context == Event.SPAWNED:
            # floating will trigger on_floated to do the actual alignment
            self.send(c.float_)
            return
        ws = self.focused_ws if context == Event.KEYBIND else self.con_ws
        r = self.con_rect(Rect.from_i3ipc(ws.rect))
        if context == Event.KEYBIND:
            self.send(
                c.fetch.format(ws.name),
                c.resize_abs.format(r.w, r.h),
                c.move_abs.format(r.x, r.y),
                c.focus,
            )
        else:
            self.send(c.resize_abs.format(r.w, r.h), c.move_abs.format(r.x, r.y))
