#!/usr/bin/env python3

import enum

import i3ipc

# TODO:
#   - watch for term being sent to another display; execute fetch() on event in order to resize/position properly
#   - make keybind matching more robust (verify mod etc)...
#   - argparse for keybind/name/pos/size
#   - investigate issue with on_spawn() not triggering if registered from within spawn() (delayed registration?)


class Pos(enum.Enum):
    TOP = enum.auto()
    BOTTOM = enum.auto()
    LEFT = enum.auto()
    RIGHT = enum.auto()


class Shape:
    def __init__(self, minor, major):
        if max(minor, major) > 1.0 or min(minor, major) < 0.0:
            raise ValueError(f"Shape out of range [0,1]: minor={minor}, major={major}")
        self.minor = minor
        self.major = major


CONF = {
    "keybind": "n",
    "name": "kitti3",
    "size": Shape(minor=0.4, major=1.0),
    "pos": Pos.RIGHT,
}


class Kitti3:
    def __init__(self, keybind: str, name: str, size: Shape, pos: Pos):
        self.keybind = keybind
        self.name = name
        self.size = size
        self.pos = pos

        self.i3 = i3ipc.Connection()
        self.set_keybind()
        self.i3.on("binding", self.on_keybind)
        self.i3.on("window::new", self.on_spawned)
        self.i3.on("shutdown::exit", self.on_shutdown_exit)
        # self.i3.on("window::move", self.on_moved)

    def loop(self):
        try:
            self.i3.main()
        finally:
            self.i3.main_quit()

    def set_keybind(self):
        print("binding...")
        res = self.i3.command(f"bindsym $mod+{self.keybind} nop kitti3_ipc")
        print(res[0].success)
        print(res[0].error)

    def on_keybind(self, _, be):
        if be.binding.input_type == "keyboard" and be.binding.symbol == self.keybind:
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
        self.i3.command(f"exec --no-startup-id kitty --name {self.name}")

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

        if self.pos in (Pos.TOP, Pos.BOTTOM):
            width = round(ws.rect.width*self.size.major)
            height = round(ws.rect.height*self.size.minor)
            x = ws.rect.x
            y = ws.rect.y if self.pos is Pos.TOP else ws.rect.y + ws.rect.height - height
        else:  # LEFT || RIGHT
            width = round(ws.rect.width*self.size.minor)
            height = round(ws.rect.height*self.size.major)
            x = ws.rect.x if self.pos is Pos.LEFT else ws.rect.x + ws.rect.width - width
            y = ws.rect.y

        self.i3.command(f"[con_id={id_}] "
                        f"resize set {width}px {height}px, "
                        f"move absolute position {x}px {y}px, "
                        "move scratchpad, "
                        "scratchpad show")

    @staticmethod
    def on_shutdown_exit(_, se):
        exit(0)


def cli():
    kitti3 = Kitti3(**CONF)
    kitti3.loop()


if __name__ == "__main__":
    cli()
