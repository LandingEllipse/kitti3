#!/usr/bin/env python3

import argparse
import enum
import sys
from typing import Tuple, List, Optional

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
    def __init__(self, x, y):
        if max(x, y) > 1.0 or min(x, y) < 0.0:
            raise ValueError(f"shape out of range [0,1]: x={x}, y={y}")
        self.x = x
        self.y = y


class Kitti3:
    def __init__(self, name: str, shape: Shape, pos: Position, kitty_argv: list = None):
        self.name = name
        self.shape = shape
        self.pos = pos
        self.kitty_argv = kitty_argv

        self.con_id = None
        self.con_ws = None
        self.focused_ws = None

        self.i3 = i3ipc.Connection()
        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("window::floating", self.on_floated)
        self.i3.on("window::move", self.on_moved)
        self.i3.on("shutdown::exit", self.on_shutdown)

        # FIXME: half-baked way of checking what WM we're running on.
        self.sway = "sway" in self.i3.socket_path or self.i3.get_version().major < 3
        self.align_to_ws = self._align_to_ws_sway if self.sway else self._align_to_ws_i3

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
            self.align_to_ws(fetch=True)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Float the Kitty window once it has settled after spawning.

        The act of floating will trigger `on_floated()`, which will take care of
        alignment.
        """
        con = we.container
        if self.name not in (con.window_instance, con.app_id):
            return
        self.con_id = con.id
        self._refresh()
        self.i3.command(f"[con_id={self.con_id}] floating enable, border none")

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
        self.align_to_ws(fetch=False)

    def on_moved(self, _, we: i3ipc.WindowEvent) -> None:
        """Ensure that the Kitty window is positioned and resized when moved to a
        different sized workspace (e.g. on a different monitor).

        If Kitty has been manually tiled by the user it will not be re-floated.
        """
        con = we.container
        # skip if sway until compatibility issue is resolved
        if self.sway or not (
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
        self.align_to_ws(fetch=False)

    @staticmethod
    def on_shutdown(_, se: i3ipc.ShutdownEvent):
        exit(0)

    def spawn(self) -> None:
        """Spawn a new Kitty window associated with the name of this Kitti3 instance."""
        arg = "class" if self.sway else "name"
        cmd_base = f"exec --no-startup-id kitty --{arg} {self.name}"
        if self.kitty_argv is None:
            cmd = cmd_base
        else:
            argv = " ".join(self.kitty_argv)
            cmd = f"{cmd_base} {argv}"
        self.i3.command(cmd)

    def _align_to_ws_i3(self, fetch: bool = True) -> None:
        """Adapt the dimensions and location of Kitty's window to the `ws` workspace.

        If `fetch` is True, Kitty will be moved from its current workspace to the
        focused workspace via the scratchpad (where it might already reside).

        TODO:
            - We can skip alignment if src and dest WSs are on the same output, and
              kitty has not gone from tiled to floating (i.e. this method was not called
              from `on_float()`). However, it's probably not worth the hassle to track.
        """
        if self.con_id is None:
            raise RuntimeError("internal error: Kitty instance ID not yet assigned")
        ws = self.focused_ws if fetch else self.con_ws
        width = round(ws.rect.width * self.shape.x)
        height = round(ws.rect.height * self.shape.y)
        x = {
            "L": ws.rect.x,
            "C": ws.rect.x + round((ws.rect.width / 2) - (width / 2)),
            "R": ws.rect.x + ws.rect.width - width,
        }[self.pos.x]
        y = {
            "T": ws.rect.y,
            "C": ws.rect.y + round((ws.rect.height / 2) - (height / 2)),
            "B": ws.rect.y + ws.rect.height - height,
        }[self.pos.y]

        self.i3.command(
            f"[con_id={self.con_id}]"
            f"{f' move container to workspace {ws.name},' if fetch else ''}"
            f" resize set {width}px {height}px,"
            f" move absolute position {x}px {y}px,"
            f"{ 'focus' if fetch else ''}"
        )

    def _align_to_ws_sway(self, fetch: bool = True) -> None:
        """Adapt the dimensions and location of Kitty's window to the `ws` workspace.

        If `fetch` is True, Kitty will be moved from its current workspace to the
        focused workspace.
        """
        if self.con_id is None:
            raise RuntimeError("internal error: Kitty instance ID not yet assigned")
        ws = self.focused_ws if fetch else self.con_ws

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

        self.i3.command(
            f"[con_id={self.con_id}]"
            f"{f' move container to workspace {ws.name},' if fetch else ''}"
            f" resize set {width}ppt {height}ppt,"
            f" move position {x}ppt {y}ppt,"
            f"{ 'focus' if fetch else ''}"
        )

    def _refresh(self) -> None:
        """Update the information on the presence of the associated Kitty instance,
        its workspace and the focused workspace.
        """
        tree = self.i3.get_tree()
        if self.con_id is None:
            for con in tree:
                if self.name in (con.window_instance, con.app_id):
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
            "Kitti3: i3wm drop-down manager for Kitty. Arguments following '--' are"
            " forwarded to the Kitty instance"
        )
    )
    ap.set_defaults(**defaults)
    ap.add_argument(
        "-n",
        "--name",
        help=(
            "name/tag used to identify this Kitti3 instance. Must match the keybinding"
            " used in the i3wm config (e.g. `bindsym $mod+n nop NAME`)"
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
    kitti3 = Kitti3(
        name=args.name,
        shape=args.shape,
        pos=args.position,
        kitty_argv=argv_kitty,
    )
    kitti3.loop()


if __name__ == "__main__":
    cli()
