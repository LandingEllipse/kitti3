#!/usr/bin/env python3

import argparse
import enum
import functools
import sys
import time
from collections import namedtuple
from typing import List, Optional, Tuple

import i3ipc

try:
    from . import __version__
except ImportError:
    __version__ = "N/A"


DEFAULTS = {
    "name": "kitti3",
    "shape": (1.0, 0.4),
    "position": "RIGHT",
}

CLIENTS = {
    "kitty": {
        "i3": {
            "cmd": "--no-startup-id kitty --name {}",
            "crit_attr": "window_instance",
        },
        "sway": {
            "cmd": "kitty --class {}",
            "crit_attr": "app_id",
        },
    },
}


class Position(enum.Enum):
    LT = TL = LEFT = TOP = enum.auto()
    LC = CL = enum.auto()
    LB = BL = BOTTOM = enum.auto()
    CT = TC = enum.auto()
    CC = enum.auto()
    CB = BC = enum.auto()
    RT = TR = RIGHT = enum.auto()
    RC = CR = enum.auto()
    RB = BR = enum.auto()

    def __init__(self, _):
        self.compat: bool = False

    def __str__(self):
        return self.name

    @classmethod
    def from_str(cls, val):
        try:
            pos = cls[val.upper()]
        except KeyError:
            raise ValueError(f"'{val}' is not a valid position") from None
        if val.upper() in ("LEFT", "RIGHT"):
            pos.compat = True
        return pos

    @property
    def x(self):
        return self.name[0]

    @property
    def y(self):
        return self.name[1]


class Shape:
    def __init__(self, x: float, y: float):
        if max(x, y) > 1.0 or min(x, y) < 0.0:
            raise ValueError(f"shape out of range [0,1]: x={x}, y={y}")
        self.x: float = x
        self.y: float = y


Client = namedtuple("Client", ["cmd", "crit_attr"])


class Trigger(enum.Enum):
    KEYBIND = enum.auto()
    SPAWNED = enum.auto()
    FLOATED = enum.auto()
    MOVED = enum.auto()


class Rect(namedtuple("Rect", ["x", "y", "w", "h"])):
    __slots__ = ()

    @classmethod
    def from_i3ipc(cls, r: i3ipc.Rect):
        return cls(r.x, r.y, r.width, r.height)


class Kitt:
    def __init__(
        self,
        conn: i3ipc.Connection,
        name: str,
        shape: Shape,
        pos: Position,
        client: Client,
        client_argv: List[str] = None,
    ):
        self.i3 = conn
        self.name = name
        self.shape = shape
        self.pos = pos
        self.client = client
        self.client_argv = client_argv

        self.con_id: Optional[str] = None
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
        """Toggle the visibility of Kitti3's Kitty instance when the appropriate keybind
        command is triggered by the user.

        Hide the Kitty window if it is present on the focused workspace, otherwise
        fetch it from its current workspace (scratchpad or regular). Spawn a new Kitty
        instance if one does not presently exist.
        """
        if be.binding.command != f"nop {self.name}":
            return
        self._refresh()
        if self.con_id is None:
            self.spawn()
        elif self.con_ws.name == self.focused_ws.name:
            self.i3.command(f"[con_id={self.con_id}] floating enable, move scratchpad")
        else:
            self.align_to_ws(Trigger.KEYBIND)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Float the Kitty window once it has settled after spawning.

        The act of floating will trigger `on_floated()`, which will take care of
        alignment.
        """
        con = we.container
        if getattr(con, self.client.crit_attr) != self.name:
            return
        self.con_id = con.id
        self._refresh()
        self.align_to_ws(Trigger.SPAWNED)

    def on_floated(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the Kitty window is aligned to its workspace when transitioning
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
        # despawn guard + toggle-while-tiled trigger repression
        if self.con_ws is None or self.con_ws.name == "__i3_scratch":
            return
        self.align_to_ws(Trigger.FLOATED)

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the Kitty window is positioned and resized when moved to a
        different sized workspace (e.g. on a different monitor).

        If Kitty has been manually tiled by the user it will not be re-floated.
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
        if (
            # deswpawn guard (sway)
            None in (self.con_ws, self.focused_ws)
            # avoid double-triggering
            or self.con_ws.name == self.focused_ws.name
            # avoid triggering on a move to the scratchpad
            or self.con_ws.name == "__i3_scratch"
        ):
            return
        self.align_to_ws(Trigger.MOVED)

    @staticmethod
    def on_shutdown(_, se: i3ipc.ShutdownEvent):
        exit(0)

    def spawn(self) -> None:
        """Spawn a new client window associated with the name of this Kitti3 instance."""
        cmd = f"exec {self.client.cmd.format(self.name)}"
        if self.client_argv:
            cmd = f"{cmd} {' '.join(self.client_argv)}"
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

    def _refresh(self) -> None:
        """Update the information on the presence of the associated Kitty instance,
        its workspace and the focused workspace.
        """
        tree = self.i3.get_tree()
        if self.con_id is None:
            for con in tree:
                if getattr(con, self.client.crit_attr) == self.name:
                    self.con_id = con.id
                    self.con_ws = con.workspace()
                    break
            else:
                self.con_ws = None
        else:
            try:
                self.con_ws = tree.find_by_id(self.con_id).workspace()
            # The Kitty instance has despawned since the last refresh
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

    def align_to_ws(self, context: Trigger) -> None:
        raise NotImplementedError


class Kitts(Kitt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sway_timing_compat = True  # TODO: add as arg

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        # Currently under Sway, if a container on an inactive workspace is resized, it
        # is forcibly reparented to its output's active workspace. Therefore, this
        # feature is diabled, pending https://github.com/swaywm/sway/issues/6465 .
        return

    def spawn(self) -> None:
        r = self.con_rect()
        self.i3.command(
            f"for_window [app_id={self.name}] 'floating enable, border none, resize set"
            f" {r.w}ppt {r.h}ppt, move position {r.x}ppt {r.y}ppt'"
        )
        super().spawn()

    def align_to_ws(self, context: Trigger) -> None:
        if context == Trigger.SPAWNED:
            return
        if context == Trigger.FLOATED and self.sway_timing_compat:
            time.sleep(0.02)
        r = self.con_rect()
        crit = f"[con_id={self.con_id}]"
        resize = f"resize set {r.w}ppt {r.h}ppt"
        move = f"move position {r.x}ppt {r.y}ppt"
        if context == Trigger.KEYBIND:
            fetch = f"move container to workspace {self.focused_ws.name}"
            cmd = f"{crit} {fetch}, {resize}, {move}, focus"
        else:
            cmd = f"{crit} {resize}, {move}"
        self.i3.command(cmd)


class Kitti3(Kitt):
    def align_to_ws(self, context: Trigger) -> None:
        # Under i3, in multi-output configurations, a ppt move is considered relative
        # to the rect defined by the bounding box of all outputs, not by the con's
        # workspace. Yes, this is madness, and so we have to do absolute moves (and
        # therefore we also do absolute resizes to stay consistent).
        if context == Trigger.SPAWNED:
            # floating will trigger on_floated to do the actual alignment
            self.i3.command(f"[con_id={self.con_id}] floating enable, border none")
            return
        ws = self.focused_ws if context == Trigger.KEYBIND else self.con_ws
        r = self.con_rect(Rect.from_i3ipc(ws.rect))
        crit = f"[con_id={self.con_id}]"
        resize = f"resize set {r.w}px {r.h}px"
        move = f"move absolute position {r.x}px {r.y}px"
        if context == Trigger.KEYBIND:
            fetch = f"move container to workspace {ws.name}"
            cmd = f"{crit} {fetch}, {resize}, {move}, focus"
        else:
            cmd = f"{crit} {resize}, {move}"
        self.i3.command(cmd)


def _split_args(args: List[str]) -> Tuple[List, Optional[List]]:
    try:
        split = args.index("--")
        return args[:split], args[split + 1 :]
    except ValueError:
        return args, None


def _simple_fraction(arg: str) -> float:
    arg = float(arg)
    if not 0 <= arg <= 1:
        raise argparse.ArgumentError(
            "argument must be a simple fraction, within [0, 1]"
    )
    return arg


def _parse_args(argv: List[str], defaults: dict) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Kitti3: i3/Sway drop-down manager for Kitty. Arguments following '--' are"
            " forwarded to the Kitty instance"
        )
    )
    ap.set_defaults(**defaults)
    ap.add_argument(
        "-n",
        "--name",
        help=(
            "name/tag used to identify this Kitti3 instance. Must match the keybinding"
            " used in the i3/Sway config (e.g. `bindsym $mod+n nop NAME`)"
        ),
    )
    ap.add_argument(
        "-p",
        "--position",
        type=Position.from_str,
        choices=list(Position),
        help=(
            "where to position the Kitty window within the active workspace, e.g. 'TL'"
            " for Top Left, or 'BC' for Bottom Center (character order does not matter)"
        ),
    )
    ap.add_argument(
        "-s",
        "--shape",
        type=_simple_fraction,
        nargs=2,
        help=(
            "dimensions (x, y) of the Kitty window, each as a fraction of the workspace"
            " size, e.g. '1.0 0.5' for full width, half height. Note: for backwards"
            " compatibility, if POSITION is 'left' or 'right' (default), the dimensions"
            " are reversed (y, x)"
        ),
    )
    ap.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show %(prog)s's version number and exit",
    )

    args = ap.parse_args(argv)

    if args.position.compat:
        args.shape = Shape(*reversed(args.shape))
    else:
        args.shape = Shape(*args.shape)

    return args


def cli() -> None:
    argv_kitti3, argv_kitty = _split_args(sys.argv[1:])
    args = _parse_args(argv_kitti3, DEFAULTS)

    conn = i3ipc.Connection()
    # FIXME: half-baked way of checking what WM we're running on.
    sway = "sway" in conn.socket_path or conn.get_version().major < 3
    _Kitt = Kitts if sway else Kitti3
    # TODO: add arg --client and --crit-attr:
    #   - if client is single word, create Client from CLIENTS lookup,
    #   - else ensure name placeholder in client and use w/--crit-attr to create Client
    #   - client should default to 'kitty'
    #   - crit-attr should default to window_instance for i3 and app_id for sway
    client = Client(**CLIENTS["kitty"]["sway" if sway else "i3"])

    kitt = _Kitt(
        conn=conn,
        name=args.name,
        shape=args.shape,
        pos=args.position,
        client=client,
        client_argv=argv_kitty,
    )
    kitt.loop()


if __name__ == "__main__":
    cli()
