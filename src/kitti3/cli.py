import argparse
import sys
from typing import Iterable, List, Optional, Tuple

import i3ipc

from .kitt import Kitti3, Kitts
from .util import CritAttr, Position, Shape, Client

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
    sway = "sway" in conn.socket_path  # or conn.get_version().major < 3
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
