#!/usr/bin/env python3

import argparse
import enum
import functools
import sys
import time
from typing import Iterable, List, NamedTuple, Optional, Tuple

import i3ipc

try:
    from . import __version__
except ImportError:
    __version__ = "N/A"



def quote_arg(arg: str) -> str:
    if " " in arg:
        arg = f'"{arg}"'
    return arg


class CritAttr(enum.Enum):
    """Criteria attributes used to target client instances.

    Values are the corresponding i3ipc.Container attribute names.
    """

    APP_ID = "app_id"
    CLASS = "window_class"
    # CON_MARK = "marks"
    INSTANCE = "window_instance"
    TITLE = "name"

    def __str__(self):
        return self.name.lower()

    @classmethod
    def from_str(cls, name):
        try:
            attr = cls[name.upper()]
        except KeyError:
            raise ValueError(f"'{name}' is not a valid criterium attribute") from None
        return attr


class Client(NamedTuple):
    cmd: str
    cattr: CritAttr


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
    def from_str(cls, name) -> "Position":
        try:
            pos = cls[name.upper()]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"'{name}' is not a valid position"
            ) from None
        if name.upper() in ("LEFT", "RIGHT"):
            pos.compat = True
        return pos

    @property
    def x(self):
        return self.name[0]

    @property
    def y(self):
        return self.name[1]


class Rect(NamedTuple):
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_i3ipc(cls, r: i3ipc.Rect):
        return cls(r.x, r.y, r.width, r.height)


class Shape(NamedTuple):
    x: float
    y: float

    @classmethod
    def from_strs(cls, strs: List[str], compat: bool = False) -> "Shape":
        fracts = [cls._proper_fraction(v) for v in strs]
        if compat:
            fracts = reversed(fracts)
        return cls(*fracts)

    @staticmethod
    def _proper_fraction(arg: str) -> float:
        val = None
        try:
            val = float(arg)
        except ValueError as e:
            val = e
            factors = arg.split("/")
            if len(factors) == 2:
                try:
                    val = float(factors[0]) / float(factors[1])
                except (ValueError, ZeroDivisionError) as e:
                    val = e
            if isinstance(val, Exception):
                raise argparse.ArgumentTypeError(f"'{arg}': {val}") from None
        if not (0 <= val <= 1):
            raise argparse.ArgumentTypeError(
                f"'{arg}': {val:.3f} is not in the range [0, 1]"
            )
        return val


class Trigger(enum.Enum):
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
        self._refresh()
        if self.con_id is None:
            self.spawn()
        elif self.con_ws.name == self.focused_ws.name:
            self.i3.command(f"[con_id={self.con_id}] floating enable, move scratchpad")
        else:
            self.align_to_ws(Trigger.KEYBIND)

    def on_spawned(self, _, we: i3ipc.WindowEvent) -> None:
        """Float the client window once it has settled after spawning.

        The act of floating will trigger `on_floated()`, which will take care of
        alignment.
        """
        con = we.container
        if getattr(con, self.client.cattr.value) != self.name:
            return
        self.con_id = con.id
        self._refresh()
        self.align_to_ws(Trigger.SPAWNED)

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
        # despawn guard + toggle-while-tiled trigger repression
        if self.con_ws is None or self.con_ws.name == "__i3_scratch":
            return
        self.align_to_ws(Trigger.FLOATED)

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
        if self.client.cmd is None:
            return
        cmd = f"exec {self.client.cmd.format(quote_arg(self.name))}"
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
            f"for_window [{self.client.cattr}={quote_arg(self.name)}] 'floating enable,"
            f" border none, resize set {r.w}ppt {r.h}ppt, move position {r.x}ppt"
            f" {r.y}ppt'"
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


DEFAULTS = {
    "name": "kitti3",
    "shape": (1.0, 0.4),
    "position": "RIGHT",
}

CLIENTS = {
    "kitty": {
        "i3": {
            "cmd": "--no-startup-id kitty --name {}",
            "cattr": CritAttr.INSTANCE,
        },
        "sway": {
            "cmd": "kitty --class {}",
            "cattr": CritAttr.APP_ID,
        },
    },
    "alacritty": {
        "i3": {
            "cmd": "--no-startup-id alacritty --class {}",
            "cattr": CritAttr.INSTANCE,
        },
        "sway": {
            "cmd": "alacritty --class {}",
            "cattr": CritAttr.APP_ID,
        },
    },
    "firefox": {
        "i3": {
            "cmd": "firefox --class {}",
            "cattr": CritAttr.CLASS,
        },
        "sway": {
            "cmd": "GDK_BACKEND=wayland firefox --name {}",
            "cattr": CritAttr.APP_ID,
        },
    },
}


class _ListClientsAction(argparse.Action):
    def __init__(
        self,
        option_strings,
        dest=argparse.SUPPRESS,
        default=argparse.SUPPRESS,
        help=None,
    ):
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        print("Kitti3 known clients")
        for client, wms in CLIENTS.items():
            print(f"\n{client}")
            for wm, props in wms.items():
                print(f"  {wm}")
                for prop, val in props.items():
                    print(f"    {prop}: {val}")
        parser.exit()


def _split_args(args: List[str]) -> Tuple[List, Optional[List]]:
    try:
        split = args.index("--")
        return args[:split], args[split + 1 :]
    except ValueError:
        return args, None


def _format_choices(choices: Iterable):
    choice_strs = ",".join([str(choice) for choice in choices])
    return f"{{{choice_strs}}}"


def _parse_args(argv: List[str], defaults: dict) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Kitti3: i3/sway floating window handler. Arguments following '--' are"
            " forwarded to the client when spawning"
        )
    )
    ap.set_defaults(**defaults)
    ap.add_argument(
        "-a",
        "--cattr",
        type=CritAttr.from_str,
        choices=list(CritAttr),
        help=(
            f"CATTR ({_format_choices(list(CritAttr))}): criterium attribute used to"
            " match a CLIENT instance to its NAME. Only required if a custom"
            " expression is provided for CLIENT. If CATTR is provided but no CLIENT,"
            " spawning is diabled and assumed to be handled by the user"
        ),
        metavar="",
    )
    _cl = ap.add_argument(
        "-c",
        "--client",
        dest="cmd",
        help=(
            f"CLIENT (cmd exp. or {_format_choices(CLIENTS.keys())}): a custom command"
            " expression or shorthand for one of Kitti3's known clients. For the"
            " former, a placeholder for NAME is required, e.g. 'myapp --class {}"
        ),
        metavar="",
    )
    ap.add_argument(
        "-n",
        "--name",
        help=(
            "NAME (string): name used to identify the CLIENT via CATTR. Must match the"
            " keybinding used in the i3/Sway config (e.g. `bindsym $mod+n nop NAME`)"
        ),
        metavar="",
    )
    ap.add_argument(
        "-p",
        "--position",
        type=Position.from_str,
        choices=list(Position),
        help=(
            f"POSITION ({_format_choices(list(Position))}): where to position the"
            " client window within the workspace, e.g. 'TL' for Top Left, or 'BC' for"
            " Bottom Center (character order does not matter)"
        ),
        metavar="",
    )
    _sh = ap.add_argument(
        "-s",
        "--shape",
        nargs=2,
        help=(
            "SHAPE SHAPE (x and y dimensions): size of the client window relative to"
            " its workspace. Values can be given as decimals or fractions, e.g., '1"
            " 0.25' and '1.0 1/4' are both interpreted as full width, quarter height."
            " Note: for backwards compatibility, if POSITION is 'left' or 'right'"
            " (default), the dimensions are interpreted in (y, x) order"
        ),
        metavar="",
    )
    ap.add_argument(
        "--list-clients",
        action=_ListClientsAction,
        help="list Kitti3's known clients and their command expressions",
    )
    ap.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show %(prog)s's version number and exit",
    )
    args = ap.parse_args(argv)

    try:
        args.shape = Shape.from_strs(args.shape, args.position.compat)
    except argparse.ArgumentTypeError as e:
        ap.error(str(argparse.ArgumentError(_sh, str(e))))

    if args.cmd is None:
        # default to Kitty for backwards compatibility
        if args.cattr is None:
            args.cmd = "kitty"
    elif args.cmd not in CLIENTS:
        if args.cattr is None:
            msg = (
                f"'{args.cmd}' is not a known client; if it is a custom expression,"
                " CATTR must also be provided"
            )
            ap.error(str(argparse.ArgumentError(_cl, msg)))
        elif "{}" not in args.cmd:
            msg = (
                f"custom client expression '{args.cmd}' must contain a '{{}}'"
                " placeholder for NAME"
            )
            ap.error(str(argparse.ArgumentError(_cl, msg)))

    return args


def cli() -> None:
    argv_kitti3, argv_client = _split_args(sys.argv[1:])
    args = _parse_args(argv_kitti3, DEFAULTS)

    # FIXME: half-baked way of checking what WM we're running on.
    conn = i3ipc.Connection()
    sway = "sway" in conn.socket_path or conn.get_version().major < 3
    _Kitt = Kitts if sway else Kitti3

    if args.cmd in CLIENTS:
        c = CLIENTS[args.cmd]["sway" if sway else "i3"]
        args.cmd = c["cmd"]
        args.cattr = c["cattr"]
    client = Client(args.cmd, args.cattr)

    kitt = _Kitt(
        conn=conn,
        name=args.name,
        shape=args.shape,
        pos=args.position,
        client=client,
        client_argv=argv_client,
    )
    kitt.loop()


if __name__ == "__main__":
    cli()
