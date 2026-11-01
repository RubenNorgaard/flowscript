"""32-bit integer semantics shared by the compiler (constant folding) and the Python VM.

They match the C VM exactly: wrap-around, truncating division, masked shifts.
"""


class DivZero(Exception):
    pass


def wrap(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def div(a, b):
    if b == 0:
        raise DivZero()
    q = abs(a) // abs(b)
    return wrap(-q if (a < 0) != (b < 0) else q)


def mod(a, b):
    if b == 0:
        raise DivZero()
    return wrap(a - div(a, b) * b)


def shl(a, b):
    return wrap((a & 0xFFFFFFFF) << (b & 31))


def shr(a, b):
    return wrap(a >> (b & 31))


BINARY = {
    "+": lambda a, b: wrap(a + b),
    "-": lambda a, b: wrap(a - b),
    "*": lambda a, b: wrap(a * b),
    "/": div,
    "%": mod,
    "&": lambda a, b: wrap(a & b),
    "|": lambda a, b: wrap(a | b),
    "^": lambda a, b: wrap(a ^ b),
    "<<": shl,
    ">>": shr,
    "==": lambda a, b: int(a == b),
    "!=": lambda a, b: int(a != b),
    "<": lambda a, b: int(a < b),
    "<=": lambda a, b: int(a <= b),
    ">": lambda a, b: int(a > b),
    ">=": lambda a, b: int(a >= b),
    "and": lambda a, b: int(bool(a) and bool(b)),
    "or": lambda a, b: int(bool(a) or bool(b)),
    "xor": lambda a, b: int(bool(a) != bool(b)),
}

UNARY = {
    "-": lambda a: wrap(-a),
    "~": lambda a: wrap(~a),
    "not": lambda a: int(not a),
}
