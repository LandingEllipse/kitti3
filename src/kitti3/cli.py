import argparse
import logging
import sys
from typing import Callable, Iterable, List, Optional, Tuple, Type, TypeVar

import i3ipc

from .kitt import Kitti3, Kitts
from .util import AnimParams, Client, Cattr, Pos, Shape

try:
    from . import __version__
except ImportError:
    __version__ = "N/A"


DEFAULTS = {
    "crosstalk_delay": 0.015,
    "name": "kitti3",
    "shape": (1.0, 0.4),
    "position": "RIGHT",
    "anim_show": 0.1,
    "anim_hide": 0.1,
    "anim_fps": 60,
}

CLIENTS = {
    "kitty": {
        "i3": {
            "cmd": "--no-startup-id kitty --name {}",
            "cattr": Cattr.INSTANCE,
        },
        "sway": {
            "cmd": "kitty --class {}",
            "cattr": Cattr.APP_ID,
        },
    },
    "alacritty": {
        "i3": {
            "cmd": "--no-startup-id alacritty --class {}",
            "cattr": Cattr.INSTANCE,
        },
        "sway": {
            "cmd": "alacritty --class {}",
            "cattr": Cattr.APP_ID,
        },
    },
    "firefox": {
        "i3": {
            "cmd": "firefox --class {}",
            "cattr": Cattr.CLASS,
        },
        "sway": {
            "cmd": "GDK_BACKEND=wayland firefox --name {}",
            "cattr": Cattr.APP_ID,
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
        for client, hosts in CLIENTS.items():
            print(f"\n{client}")
            for host, props in hosts.items():
                print(f"  {host}")
                for prop, val in props.items():
                    print(f"    {prop}: {val}")
        parser.exit()


def _try_ipc(conn: i3ipc.Connection, cmd: str):
    try:
        conn.command(cmd)
    except BrokenPipeError:
        pass


def _split_args(args: List[str]) -> Tuple[List, Optional[List]]:
    try:
        split = args.index("--")
        return args[:split], args[split + 1 :]
    except ValueError:
        return args, None


def _format_choices(choices: Iterable):
    choice_strs = ",".join([str(choice) for choice in choices])
    return f"{{{choice_strs}}}"


T = TypeVar("T", int, float)


def _num_in(type_: Type[T], min_: T, max_: T) -> Callable[[str], T]:
    def validator(arg: str) -> T:
        try:
            val = type_(arg)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"'{arg}': {e}") from None
        if not (min_ <= val <= max_):
            raise argparse.ArgumentTypeError(
                f"'{arg}': {val} is not in the range [{min_}, {max_}]"
            )
        return val

    return validator


def _parse_args(argv: List[str], host: str, defaults: dict) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        add_help=False,
        description=(
            "Kitti3: i3/sway floating window handler. Arguments following '--' are"
            " forwarded to the client when spawning"
        ),
    )
    ap.set_defaults(**defaults)

    ag_look = ap.add_argument_group(title="look and feel")
    ag_look.add_argument(
        "-a",
        "--animate",
        action="store_true",
        help="[flag] enable slide-in animation",
    )
    ag_look.add_argument(
        "-p",
        "--position",
        type=Pos.from_str,
        choices=list(Pos),
        help=(
            f"POSITION ({_format_choices(list(Pos))}, default:"
            f" '{DEFAULTS['position']}'): where to position the client window within"
            " the workspace, e.g. 'TL' for Top Left, or 'BC' for Bottom Center"
            " (first character anchors animation)"
        ),
        metavar="",
    )
    _sh = ag_look.add_argument(
        "-s",
        "--shape",
        nargs=2,
        help=(
            "SHAPE SHAPE (x y, default:"
            f" '{' '.join(str(s) for s in reversed(DEFAULTS['shape']))}'): size of the"
            " client window relative to its workspace. Values can be given as decimals"
            " or fractions, e.g., '1 0.25' and '1.0 1/4' are both interpreted as full"
            " width, quarter height. Note: for backwards compatibility, if POSITION is"
            " 'left' or 'right' (default), the dimensions are interpreted in (y, x)"
            " order"
        ),
        metavar="",
    )
    _anim_show = ag_look.add_mutually_exclusive_group()
    _anim_show.add_argument(
        "--anim-show",
        type=_num_in(float, 0.01, 1),
        help=(
            f"DURATION ([0.01, 1], default: {DEFAULTS['anim_show']}):"
            " duration of animated slide-in. Disable with --no-anim-show"
        ),
        metavar="",
    )
    _anim_show.add_argument(
        "--no-anim-show",
        action="store_const",
        const=None,
        dest="anim_show",
        help=argparse.SUPPRESS,
    )
    _anim_hide = ag_look.add_mutually_exclusive_group()
    _anim_hide.add_argument(
        "--anim-hide",
        type=_num_in(float, 0.01, 1),
        help=(
            f"DURATION ([0.01, 1], default: {DEFAULTS['anim_hide']}):"
            " duration of animated slide-out. Disable with --no-anim-hide"
        ),
        metavar="",
    )
    _anim_hide.add_argument(
        "--no-anim-hide",
        action="store_const",
        const=None,
        dest="anim_hide",
        help=argparse.SUPPRESS,
    )

    ag_look.add_argument(
        "--anim-fps",
        type=_num_in(int, 1, 100),
        help=(
            f"FPS ([1, 100], default: {DEFAULTS['anim_fps']}):"
            " target animation frames per second"
        ),
        metavar="",
    )

    ag_id = ap.add_argument_group(title="identification")
    _bs = ag_id.add_argument(
        "-b",
        "--bindsym",
        help=(
            f"KEYCOMBO (config format, default: disabled): (sway) let Kitti3"
            f" dynamically set its own keyboard shortcut to KEYCOMBO"
        ),
        metavar="",
    )

    _cl = ag_id.add_argument(
        "-c",
        "--client",
        dest="cmd",
        help=(
            f"CLIENT (expression or {_format_choices(CLIENTS.keys())}, default:"
            " 'kitty'): a custom command expression or shorthand for one of Kitti3's"
            " known clients. For the former, a placeholder for NAME is required, e.g."
            " 'myapp --class {}"
        ),
        metavar="",
    )
    ag_id.add_argument(
        "-l",
        "--loyal",
        action="store_true",
        help=(
            "[flag] once a CLIENT instance has been associated, ignore new candidates"
            " when they spawn. If CATTR is con_mark, don't validate the associated"
            " instance's mark on refresh"
        ),
    )
    ag_id.add_argument(
        "-n",
        "--name",
        help=(
            f"NAME (string, default: '{DEFAULTS['name']}'): name used to identify the"
            " CLIENT via CATTR. Must match the keybinding used in the i3/Sway config"
            " (e.g. `bindsym $mod+n nop NAME`)"
        ),
        metavar="",
    )
    ag_id.add_argument(
        "-t",
        "--cattr",
        type=Cattr.from_str,
        choices=list(Cattr),
        help=(
            f"CATTR ({_format_choices(list(Cattr))}): criterium attribute used to"
            " match a CLIENT instance to its NAME. Only required if a custom"
            " expression is provided for CLIENT. If CATTR is provided but no CLIENT,"
            " spawning is diabled and assumed to be handled by the user"
        ),
        metavar="",
    )

    ag_misc = ap.add_argument_group(title="misc / advanced")
    _crosstalk = ag_misc.add_mutually_exclusive_group()
    _crosstalk.add_argument(
        "--crosstalk-delay",
        type=_num_in(float, 0.001, 0.2),
        dest="crosstalk_delay",
        help=(
            f"MS ([0.001, 0.2], default: {DEFAULTS['crosstalk_delay']} seconds): (sway)"
            " atomic transaction crosstalk mitigation. Experiment with this if"
            " re-floated windows don't resize properly. Disable with"
            " --no-crosstalk-delay"
        ),
        metavar="",
    )
    _crosstalk.add_argument(
        "--no-crosstalk-delay",
        action="store_const",
        const=None,
        dest="crosstalk_delay",
        help=argparse.SUPPRESS,
    )
    ag_misc.add_argument(
        "--debug",
        action="store_true",
        help="[flag] enable diagnostic messages",
    )
    ag_misc.add_argument(
        "--list-clients",
        action=_ListClientsAction,
        help="[flag] show %(prog)s's known clients and exit",
    )
    ag_misc.add_argument(
        "-h",
        "--help",
        action="help",
        help="[flag] show this help message and exit",
    )
    ag_misc.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="[flag] show %(prog)s's version number and exit",
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
    if args.cmd in CLIENTS:
        c = CLIENTS[args.cmd][host]
        args.cmd = c["cmd"]
        args.cattr = c["cattr"]
    args.client = Client(args.cmd, args.cattr)

    args.anim_params = AnimParams(
        (args.animate and args.position.anchor is not None),
        args.position.anchor,
        args.anim_show,
        args.anim_hide,
        args.anim_fps,
    )

    # basic guardrails; otherwise very easy to override alnum keys if not escaping
    if args.bindsym is not None and args.bindsym.startswith("+"):
        msg = (
            f"'{args.bindsym}' looks malformed - remember to escape $ on the"
            f" commandline (e.g. '\\$mod{args.bindsym}')"
        )
        ap.error(str(argparse.ArgumentError(_bs, msg)))

    return args


def cli() -> None:
    # FIXME: half-baked way of checking what host we're running on.
    conn = i3ipc.Connection()
    host, _Kitt = {
        True: ("sway", Kitts),
        False: ("i3", Kitti3),
    }["sway" in conn.socket_path]

    argv_kitti3, argv_client = _split_args(sys.argv[1:])
    args = _parse_args(argv_kitti3, host, DEFAULTS)

    if args.debug:
        logging.basicConfig(
            datefmt="%Y-%m-%dT%H:%M:%S",
            format=(
                "%(asctime)s.%(msecs)03d %(levelname)-7s"
                " %(filename) 4s:%(lineno)03d"
                " %(name)s.%(funcName)-12s %(message)s"
            ),
            level=logging.DEBUG,
        )
    if args.bindsym is not None and host == "sway":
        cmd = f'bindsym "{args.bindsym}" "nop {args.name}"'
        ret = conn.command(cmd)[0]
        logging.debug("%s -> %s", cmd, ret.success and "OK" or ret.error)
        import atexit

        # cleanup (only effective on user exit)
        atexit.register(lambda: _try_ipc(conn, f"un{cmd}"))

    kitt = _Kitt(
        conn=conn,
        name=args.name,
        shape=args.shape,
        pos=args.position,
        client=args.client,
        client_argv=argv_client,
        anim=args.anim_params,
        loyal=args.loyal,
        crosstalk_delay=args.crosstalk_delay,
    )
    kitt.loop()
