import argparse
import enum
from typing import List, NamedTuple

import i3ipc


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
