#!/usr/bin/env python3

import sys
import enum
import argparse

import i3ipc

try:
    from . import __version__
except ImportError:
    __version__ = "N/A"


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
        return self.name.lower()

    @classmethod
    def from_str(cls, val):
        try:
            pos = cls[val.upper()]
        except KeyError:
            raise ValueError(f"'{val}' is not a valid position") from None
        if val.upper() in ("LEFT", "RIGHT"):
            pos.compat = True
        print(pos)
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


DEFAULTS = {
    "name": "kitti3",
    "shape": (1.0, 0.4),
    "position": "RIGHT",
}


class Kitti3:
    def __init__(self, name: str, shape: Shape, pos: Position, kitty_argv: list = None):
        self.name = name
        self.shape = shape
        self.pos = pos
        self.kitty_argv = kitty_argv

        self.id = None

        self.i3 = i3ipc.Connection()
        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("shutdown::exit", self.on_shutdown)
        self.i3.on("window::move", self.on_moved)

    def loop(self):
        try:
            self.i3.main()
        finally:
            self.i3.main_quit()

    def on_keybind(self, _, be):
        if be.binding.command == f"nop {self.name}":
            self.toggle()

    def _get_instance(self):
        tree = self.i3.get_tree()
        if self.id is None:
            matches = tree.find_instanced(self.name)
            instance = {m.window_instance: m for m in matches}.get(self.name, None)
            if instance is not None:
                self.id = instance.id
        else:
            instance = tree.find_by_id(self.id)
        return instance

    def toggle(self):
        kitty = self._get_instance()
        if kitty is None:
            self.spawn()
        else:
            focused_ws = self._get_focused_workspace()
            if focused_ws is None:
                print("no focused workspaces; ignoring toggle request")
                return
            if kitty.workspace().name == focused_ws.name:  # kitty present on current WS; hide
                self.i3.command(f"[con_id={self.id}] floating enable, move scratchpad")
            else:
                self.fetch(focused_ws)

    def spawn(self):
        cmd_base = f"exec --no-startup-id kitty --name {self.name}"
        if self.kitty_argv is None:
            cmd = cmd_base
        else:
            argv = " ".join(self.kitty_argv)
            cmd = f"{cmd_base} {argv}"
        self.i3.command(cmd)

    def on_spawned(self, _, we):
        if we.container.window_instance == self.name:
            self.id = we.container.id
            self.i3.command(f"[con_id={we.container.id}] "
                            "floating enable, "
                            "border none, "
                            "move scratchpad")
            self.fetch(self._get_focused_workspace())

    def on_moved(self, _, we):
        # Con is floating wrapper; the Kitty window/container is a child
        is_kitty = we.container.find_by_id(self.id)
        if not is_kitty:
            return
        focused_ws = self._get_focused_workspace()
        if focused_ws is None:
            return
        kitty = self._get_instance()  # need "fresh" instance to capture destination WS
        kitty_ws = kitty.workspace()
        if (kitty_ws is None or kitty_ws.name == focused_ws.name or
                kitty_ws.name == "__i3_scratch"):  # FIXME: fragile way to check if hidden?
            return
        self.fetch(kitty_ws, retrieve=False)

    def _get_focused_workspace(self):
        focused_workspaces = [w for w in self.i3.get_workspaces() if w.focused]
        if not len(focused_workspaces):
            return None
        return focused_workspaces[0]

    def fetch(self, ws, retrieve=True):
        if self.id is None:
            raise RuntimeError("Kitty instance ID not yet assigned")

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

        self.i3.command(f"[con_id={self.id}]"
                        f" resize set {width}px {height}px,"
                        f"{' move scratchpad, scratchpad show,' if retrieve else ''}"
                        f" move absolute position {x}px {y}px")

    @staticmethod
    def on_shutdown(_, se):
        exit(0)


def _split_args(args):
    try:
        split = args.index("--")
        return args[:split], args[split + 1:]
    except ValueError:
        return args, None


def _simple_fraction(arg):
    arg = float(arg)
    if not 0 <= arg <= 1:
        raise argparse.ArgumentError("argument needs to be a simple fraction, within"
                                     "[0, 1]")
    return arg


def _parse_args(argv, defaults):
    ap = argparse.ArgumentParser(
        description="Kitti3: i3 drop-down wrapper for Kitty."
                    " Arguments following '--' are forwarded to the Kitty instance")
    ap.set_defaults(**defaults)
    ap.add_argument("-n", "--name",
                    help="name/tag used to identify this Kitti3 instance. Must match"
                         " the keybinding used in the i3wm config (e.g."
                         " `bindsym $mod+n nop NAME`)")
    ap.add_argument("-p", "--position",
                    type=Position.from_str,
                    choices=list(Position),
                    help="where to align the origin of the Kitty window within the"
                         " active workspace, e.g. TL for Top Left, or BC for Bottom"
                         " Center (character order does not matter)")
    ap.add_argument("-s", "--shape",
                    type=_simple_fraction,
                    nargs=2,
                    help="shape of the Kitty window (x, y) dimensions as fractions"
                         " in [0, 1] of the workspace size, e.g. 1.0 0.5 for full"
                         " width, half height. Note: for backwards compatibility, if"
                         " POSITION is `left` or `right` (default), the coordinate "
                         "order is reversed")
    ap.add_argument("-v", "--version",
                    action="version",
                    version=f"%(prog)s {__version__}",
                    help="show %(prog)s's version number and exit")

    args = ap.parse_args(argv)

    if args.position.compat:
        args.shape = Shape(*reversed(args.shape))
    else:
        args.shape = Shape(*args.shape)

    return args


def cli():
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
