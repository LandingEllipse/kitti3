#!/usr/bin/env python3

import sys
import enum
import argparse

import i3ipc

# TODO:
#   - watch for term being sent to another display; execute fetch() on event in order to resize/position properly
#   - investigate issue with on_spawn() not triggering if registered from within spawn() (delayed registration?)
#   - nice-to-have: complain if Kitty isn't installed. exec command returns success even if `kitty` doesn't resolve, so need to find alternative way
#   - do verification tests of multiple instances not interfering with each other when stashing to the scratchpad


class Position(enum.Enum):
    TOP = enum.auto()
    BOTTOM = enum.auto()
    LEFT = enum.auto()
    RIGHT = enum.auto()

    def __str__(self):
        return self.name.lower()

    @staticmethod
    def from_str(pos):
        try:
            return Position[pos.upper()]
        except KeyError:
            raise ValueError(f"value '{pos}' not part of the Pos enum")


class Shape:
    def __init__(self, minor, major):
        if max(minor, major) > 1.0 or min(minor, major) < 0.0:
            raise ValueError(f"Shape out of range [0,1]: minor={minor}, major={major}")
        self.minor = minor
        self.major = major


DEFAULTS = {
    "name": "kitti3",
    # "shape": Shape(minor=0.4, major=1.0),
    "shape": (0.4, 1.0),
    "position": str(Position.RIGHT),
}


class Kitti3:
    def __init__(self, name: str, shape: Shape, pos: Position, kitty_argv: list = None):
        self.name = name
        self.shape = shape
        self.pos = pos
        self.kitty_argv = kitty_argv

        self.i3 = i3ipc.Connection()
        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("shutdown::exit", self.on_shutdown_exit)
        # self.i3.on("window::move", self.on_moved)

    def loop(self):
        try:
            self.i3.main()
        finally:
            self.i3.main_quit()

    def on_keybind(self, _, be):
        if be.binding.command == f"nop {self.name}":
            self.toggle()

    def toggle(self):
        named = self.i3.get_tree().find_instanced(self.name)
        if not len(named):
            self.spawn()
        else:
            wss = [w for w in self.i3.get_workspaces() if w.focused]
            if not len(wss):
                print("No focused workspaces; ignoring toggle request")
                return
            ws = wss[0]
            id_ = named[0].id
            if named[0].workspace().name == ws.name:  # kitty present on current WS; hide
                self.i3.command(f"[con_id={id_}] floating enable, move scratchpad")
            else:
                self.fetch(id_, ws)

    def spawn(self):
        print("\tin spawn")
        cmd_base = f"exec --no-startup-id kitty --name {self.name}"
        if self.kitty_argv is None:
            cmd = cmd_base
        else:
            argv = " ".join(self.kitty_argv)
            cmd = f"{cmd_base} {argv}"
        print(cmd)
        self.i3.command(cmd)

    def on_spawned(self, _, we):
        if we.container.window_instance == self.name:
            self.i3.command(f"[con_id={we.container.id}] "
                            "floating enable, "
                            "border none, "
                            "move scratchpad")
            self.fetch(we.container.id)

    # def on_moved(self, _, we):
    #     print("\non_moved")
    #     print(f"\tchange: {we.change}")
    #     container = we.container.descendants()[0]
    #     if not container:
    #         print("no child container; return")
    #         return
    #
    #     cur_ws = [w for w in self.i3.get_workspaces() if w.focused][0]
    #     print(f"\tcur_ws.name: {cur_ws.name}")
    #     print(f"\tcur_ws.focused: {cur_ws.focused}")
    #
    #     to_ws = we.container.workspace()
    #     if to_ws is None:
    #         print("\tempty to_ws; return")
    #         return
    #     print(f"\tto_ws.name: {to_ws.name}")
    #     print(f"\tto_ws.focused: {to_ws.focused}")
    #
    #     if container.window_instance == self.name:
    #         print("\tresize to new ws")
    #         self.fetch(container.id, container.workspace())

    def fetch(self, id_, ws=None):
        if ws is None:
            ws = [w for w in self.i3.get_workspaces() if w.focused][0]

        if self.pos in (Position.TOP, Position.BOTTOM):
            width = round(ws.rect.width * self.shape.major)
            height = round(ws.rect.height * self.shape.minor)
            x = ws.rect.x
            y = ws.rect.y if self.pos is Position.TOP else ws.rect.y + ws.rect.height - height
        else:  # LEFT || RIGHT
            width = round(ws.rect.width * self.shape.minor)
            height = round(ws.rect.height * self.shape.major)
            x = ws.rect.x if self.pos is Position.LEFT else ws.rect.x + ws.rect.width - width
            y = ws.rect.y

        self.i3.command(f"[con_id={id_}] "
                        f"resize set {width}px {height}px, "
                        f"move absolute position {x}px {y}px, "
                        "move scratchpad, "
                        "scratchpad show")

    @staticmethod
    def on_shutdown_exit(_, se):
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
        description="Kitti3 - i3 drop-down wrapper for Kitty\n\n"
                    "Arguments following '--' are forwarded to the Kitty instance")
    ap.set_defaults(**defaults)
    ap.add_argument("-n", "--name",
                    help="name/tag connecting a Kitti3 bindsym with a Kitty instance. "
                         "Forwarded to Kitty on spawn and scanned for on i3 binding "
                         "events")
    ap.add_argument("-p", "--position",
                    type=Position.from_str,
                    choices=list(Position),
                    help="Along which edge of the screen to align the Kitty window")
    ap.add_argument("-s", "--shape",
                    type=_simple_fraction,
                    nargs=2,
                    help="shape of the terminal window minor and major dimensions as a "
                         "fraction [0, 1] of the screen (note: i3bar is automatically"
                         "excluded)")

    args = ap.parse_args(argv)
    return args


def cli():
    argv_kitti3, argv_kitty = _split_args(sys.argv[1:])
    args = _parse_args(argv_kitti3, DEFAULTS)

    kitti3 = Kitti3(
        name=args.name,
        shape=Shape(*args.shape),
        pos=args.position,
        kitty_argv=argv_kitty,
    )
    kitti3.loop()


if __name__ == "__main__":
    cli()
