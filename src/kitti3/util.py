import argparse
import enum
import time
from typing import Callable, List, NamedTuple, Optional

import i3ipc


class AnimParams(NamedTuple):
    enabled: bool
    anchor: "Loc"
    show: Optional[float]
    hide: Optional[float]
    fps: int


class Cattr(enum.Enum):
    """Criteria attributes used to target client instances.

    Values are the corresponding i3ipc.Container attribute names.
    """

    APP_ID = "app_id"
    CLASS = "window_class"
    CON_MARK = "marks"
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
    cattr: Cattr


class Loc(enum.Enum):
    LEFT = L = enum.auto()
    RIGHT = R = enum.auto()
    TOP = T = enum.auto()
    BOTTOM = B = enum.auto()
    CENTER = C = enum.auto()


class Pos(enum.Enum):
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
        self.anchor: Optional[Loc] = None

    def __str__(self):
        return self.name

    @classmethod
    def from_str(cls, name) -> "Pos":
        try:
            pos = cls[name.upper()]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"'{name}' is not a valid position"
            ) from None
        if name.upper() in ("LEFT", "RIGHT"):
            pos.compat = True
        pos.anchor = cls._anchor_for(name.upper())
        return pos

    @staticmethod
    def _anchor_for(name):
        if name == "CC":
            return None
        elif name[0] == "C":
            return Loc[name[1]]
        # also works for name in ("LEFT", "RIGHT")
        return Loc[name[0]]

    @property
    def x(self):
        return Loc[self.name[0]]

    @property
    def y(self):
        return Loc[self.name[1]]


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


def animate(
    callback: Callable[[int, bool, bool], None],
    start: int,
    end: int,
    duration: float,
    fps: int,
    offset: bool,
):
    num_steps = round(duration * fps)
    if num_steps < 2:
        callback(end, True, True)
        return
    step_size = (end - start) / (num_steps - 1)
    linspaced = [round(start + step_size * (i + int(offset))) for i in range(num_steps)]
    steps = []
    for pos in linspaced:
        if pos not in steps:
            steps.append(pos)
    # compromise fps if duplicate positions removed, to ensure duration
    delay = duration / (len(steps) - 1)
    final_frame = len(steps) - 1
    t_0 = t_curr = time.time()
    for frame, pos in enumerate(steps):
        t_target = t_0 + delay * frame
        while True:
            if t_curr >= t_target:
                callback(pos, (frame == 0), (frame == final_frame))
                break
            else:
                # ok, as t_target approach prevents wakeup overhead from accumulating
                time.sleep(t_target - t_curr)
            t_curr = time.time()
