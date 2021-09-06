import enum
import functools
import logging
import time
from types import SimpleNamespace
from typing import List, Optional

import i3ipc

from .util import AnimParams, Client, Cattr, Loc, Pos, Rect, Shape, animate


class Event(enum.Enum):
    HIDE = enum.auto()
    FLOATED = enum.auto()
    MOVED = enum.auto()
    SHOW = enum.auto()
    SPAWNED = enum.auto()


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
            self.align_to_ws(Event.HIDE)
        else:
            self.align_to_ws(Event.SHOW)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Bind to a client with a criterium attribute matching Kitti3's instance name."""
        con = we.container
        if not self._cattr_matches(con):
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
        # note: cf on_moved, for i3 con is our target, but .type == "floating_con" is
        # only set on the floating wrapper. Hence the need to check .floating.
        if (con.type != "floating_con" and con.floating != "user_on") or (
            # marks are unique; want to associate to new client if mark has moved...
            not self._cattr_matches(con)
            # ...but only if we're not loyal to an existing association
            if self.client.cattr == Cattr.CON_MARK and not self.loyal
            else con.id != self.con_id
        ):
            return
        if (
            not self.refresh()
            # toggle-while-tiled trigger repression
            or self.con_ws.name == "__i3_scratch"
        ):
            return
        self.align_to_ws(Event.FLOATED)

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the client window is positioned and resized when moved to a
        different sized workspace (e.g. on a different monitor).

        If the client has been manually tiled by the user it will not be re-floated.
        """
        con = we.container
        if con.type != "floating_con" or (
            # marks are unique; want to associate to new client if mark has moved...
            not self._cattr_matches(con)
            # ...but only if we're not loyal to an existing association
            if self.client.cattr == Cattr.CON_MARK and not self.loyal
            # note: event's con is floating wrapper for i3, but target con for sway
            else (con.id != self.con_id and not con.find_by_id(self.con_id))
        ):
            return
        if (
            not self.refresh()
            # avoid double-triggering
            or self.con_ws.name == self.focused_ws.name
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
        eager = self.con_id is None or (
            self.client.cattr == Cattr.CON_MARK and not self.loyal
        )
        con = None
        for candidate in self.i3.get_tree():
            if (eager and self._cattr_matches(candidate)) or (
                not eager and candidate.id == self.con_id
            ):
                con = candidate
                break
        if con is None:
            _old_id = self.con_id
            self.con_id = self.con_ws = self.con_rect = None
            if not eager:
                self.log.info(
                    "con_id: %s has despawned; looking for an alternative", _old_id
                )
                return self.refresh()
        else:
            self.con_id = con.id
            self.con_ws = con.workspace()
            self.con_rect = Rect.from_i3ipc(con.rect)

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
    def target_rect(self, abs_ref: Rect = None) -> Rect:
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

    def _cattr_matches(self, con: i3ipc.Con) -> bool:
        cval = getattr(con, self.client.cattr.value)
        if cval is None:
            return False
        if (isinstance(cval, str) and cval == self.name) or (
            isinstance(cval, list) and self.name in cval
        ):
            return True
        return False

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
        r = self.target_rect()
        c = self.commands
        self.send_rule(c.float_, c.resize.format(r.w, r.h), c.move.format(r.x, r.y))
        super().spawn()

    def align_to_ws(self, event: Event) -> None:
        if event == Event.SPAWNED:
            return
        if event == Event.FLOATED and self.crosstalk_delay is not None:
            time.sleep(self.crosstalk_delay)
        self.log.debug(event)
        r = self.target_rect()
        c = self.commands
        if event == Event.SHOW:
            if self.anim.enabled and self.anim.show is not None:
                self._animate()
            else:
                self.send(
                    c.fetch.format(self.focused_ws.name),
                    c.resize.format(r.w, r.h),
                    c.move.format(r.x, r.y),
                    c.focus,
                )
        elif event == Event.HIDE:
            if self.anim.enabled and self.anim.hide is not None and self._undisturbed():
                self._animate(hide=True)
            else:
                self.send(self.commands.hide)
        else:
            self.send(c.resize.format(r.w, r.h), c.move.format(r.x, r.y))

    def _animate(self, hide: bool = False) -> None:
        r = self.target_rect()
        c = self.commands
        role_x, role_y, start, end = {
            Loc.LEFT: ("{}", r.y, 0 - r.w, r.x),
            Loc.RIGHT: ("{}", r.y, 100, r.x),
            Loc.TOP: (r.x, "{}", 0 - r.h, r.y),
            Loc.BOTTOM: (r.x, "{}", 100, r.y),
        }[self.anim.anchor]
        if hide:
            start, end = end, start
        move_partial = c.move.format(role_x, role_y)

        def move_cb(pos: int, first: bool, last: bool) -> None:
            # when entering, ensure first frame move lands in same transaction as fetch
            if first and not hide:
                self.send(
                    c.fetch.format(self.focused_ws.name),
                    c.resize.format(r.w, r.h),
                    move_partial.format(pos),
                    c.focus,
                )
            elif last and hide:
                self.send(self.commands.hide)
            else:
                self.send(move_partial.format(pos))

        duration = hide and self.anim.hide or self.anim.show  # type: ignore
        animate(move_cb, start, end, duration, self.anim.fps, hide)

    def _undisturbed(self) -> bool:
        tr = self.target_rect()
        cr = self.con_rect
        wr = self.focused_ws.rect
        # note: sway truncates when doing ppt->px conversion
        # (see e.g. resize.c:resize_set_floating, struct movement_amount)
        return False not in [
            cr.x == int((tr.x / 100) * wr.width + wr.x),
            cr.y == int((tr.y / 100) * wr.height + wr.y),
            cr.w == int(wr.width * (tr.w / 100)),
            cr.h == int(wr.height * (tr.h / 100)),
        ]


class Kitti3(Kitt):
    def align_to_ws(self, event: Event) -> None:
        # Under i3, in multi-output configurations, a ppt move is considered relative
        # to the rect defined by the bounding box of all outputs, not by the con's
        # workspace. Yes, this is madness, and so we have to do absolute moves (and
        # therefore we also do absolute resizes to stay consistent).
        c = self.commands
        if event == Event.SPAWNED:
            # floating will trigger on_floated to do the actual alignment
            self.send(c.float_)
            return
        ws = self.focused_ws if event == Event.SHOW else self.con_ws
        r = self.target_rect(Rect.from_i3ipc(ws.rect))
        if event == Event.SHOW:
            self.send(
                c.fetch.format(ws.name),
                c.resize_abs.format(r.w, r.h),
                c.move_abs.format(r.x, r.y),
                c.focus,
            )
        elif event == Event.HIDE:
            self.send(self.commands.hide)
        else:
            self.send(c.resize_abs.format(r.w, r.h), c.move_abs.format(r.x, r.y))
