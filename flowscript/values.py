"""Typed values: int, float, str and time. Shared by the compiler (constant folding), the
reference VM and the trace format; c/flowscript.c implements the same rules bit for bit.

    int    32-bit signed, wrap-around (intops.py)
    float  IEEE 754 double
    str    immutable byte string (UTF-8 by convention), at most STR_LEN bytes at run time
    time   milliseconds since 1970-01-01T00:00:00Z (int64, UTC, proleptic Gregorian)

Text conversions (str(x), float(s), time(s), the trace format) are defined here with
plain integer/double arithmetic so that both VMs produce identical text without a C
library: see fmt_float() and parse_float().
"""

import ctypes
import ctypes.util
import math
import struct

from .intops import wrap

INT, FLOAT, STR, TIME = 0, 1, 2, 3
TYPE_NAMES = ["int", "float", "str", "time"]
TYPE_CODES = {n: i for i, n in enumerate(TYPE_NAMES)}
NONE_TYPE = 4                      # "returns nothing" in the extern table

DAY_MS = 86400000
STR_LEN = 64                       # default max bytes of a run-time string (FS_STR_LEN)
STR_SLOTS = 16                     # default run-time string slots (FS_STR_SLOTS)


class ValueFault(Exception):
    """A run-time value error; .code is the VM fault code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


TYPE_MISMATCH, OUT_OF_STRINGS, STRING_TOO_LONG, BAD_VALUE = 10, 11, 12, 13


class Str:
    """A string value. `slot` is the VM string slot holding it (None: constant)."""
    __slots__ = ("b", "slot")

    def __init__(self, b, slot=None):
        self.b = b if isinstance(b, bytes) else str(b).encode("utf-8")
        self.slot = slot

    @property
    def text(self):
        return self.b.decode("utf-8", "replace")

    def __eq__(self, other):
        return isinstance(other, Str) and other.b == self.b

    def __hash__(self):
        return hash(self.b)

    def __repr__(self):
        return "Str(%r)" % self.b


class Time:
    __slots__ = ("ms",)

    def __init__(self, ms):
        self.ms = wrap64(ms)

    def __eq__(self, other):
        return isinstance(other, Time) and other.ms == self.ms

    def __hash__(self):
        return hash(("t", self.ms))

    def __repr__(self):
        return "Time(%s)" % fmt_time(self.ms)


def wrap64(v):
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v & (1 << 63) else v


def type_of(v):
    if isinstance(v, bool) or isinstance(v, int):
        return INT
    if isinstance(v, float):
        return FLOAT
    if isinstance(v, Str):
        return STR
    if isinstance(v, Time):
        return TIME
    raise TypeError("not a FlowScript value: %r" % (v,))


def zero(t):
    return [0, 0.0, Str(b""), Time(0)][t]


# ---- floats -----------------------------------------------------------------------------
# Exact powers of ten (10^0 .. 10^22 are exact doubles).
P10 = [float(10 ** i) for i in range(23)]


def _scale_up(m, k):
    """m * 10^k, k >= 0, as the C VM computes it."""
    while k > 22:
        m = m * 1e22
        k -= 22
    return m * P10[k]


def _scale_down(m, k):
    """m / 10^k, k >= 0."""
    while k > 22:
        m = m / 1e22
        k -= 22
    return m / P10[k]


def _decimal_exponent(a):
    """d with 10^d <= a < 10^(d+1) (approximately; corrected by the caller), a > 0 finite."""
    d = 0
    if a >= 1.0:
        while d < 400 and _scale_up(1.0, d + 1) <= a:
            d += 1
    else:
        while d > -400 and _scale_up(a, -d) < 1.0:
            d -= 1
    return d


def _digits15(a, d):
    """round(a * 10^(14-d)) as an int (15 significant digits when d is right)."""
    s = 14 - d
    m = _scale_up(a, s) if s >= 0 else _scale_down(a, -s)
    return int(m + 0.5)


def fmt_float(x):
    """Canonical text of a double: 15 significant digits, Python-like layout."""
    if x != x:
        return "nan"
    if x in (math.inf, -math.inf):
        return "inf" if x > 0 else "-inf"
    if x == 0.0:
        return "-0.0" if math.copysign(1.0, x) < 0 else "0.0"
    neg = x < 0
    a = -x if neg else x
    d = _decimal_exponent(a)
    n = _digits15(a, d)
    if n >= 10 ** 15:
        n = (n + 5) // 10
        d += 1
    elif n < 10 ** 14:
        d -= 1
        n = _digits15(a, d)
        if n >= 10 ** 15:
            n = (n + 5) // 10
            d += 1
    digits = str(n).rstrip("0") or "0"
    if -4 <= d < 15:
        if d >= 0:
            whole = digits[:d + 1].ljust(d + 1, "0")
            frac = digits[d + 1:] or "0"
            out = whole + "." + frac
        else:
            out = "0." + "0" * (-d - 1) + digits
    else:
        mant = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        out = "%se%s%02d" % (mant, "-" if d < 0 else "+", abs(d))
    return "-" + out if neg else out


def parse_float(b):
    """bytes -> float, or None if not a number. [+-]digits[.digits][(e|E)[+-]digits], nan, inf."""
    s = b.decode("latin-1") if isinstance(b, bytes) else b
    i, n = 0, len(s)
    neg = False
    if i < n and s[i] in "+-":
        neg = s[i] == "-"
        i += 1
    rest = s[i:]
    if rest in ("nan", "inf"):
        v = math.nan if rest == "nan" else math.inf
        return -v if neg and rest == "inf" else v
    # digits: the first 18 significant ones are kept in mant; exp is the power of ten to apply
    mant = 0
    ndig = 0
    exp = 0
    seen = 0
    while i < n and "0" <= s[i] <= "9":
        seen += 1
        if ndig < 18:
            mant = mant * 10 + (ord(s[i]) - 48)
            if mant:
                ndig += 1
        else:
            exp += 1
        i += 1
    if i < n and s[i] == ".":
        i += 1
        while i < n and "0" <= s[i] <= "9":
            seen += 1
            if ndig < 18:
                mant = mant * 10 + (ord(s[i]) - 48)
                if mant:
                    ndig += 1
                exp -= 1
            i += 1
    if not seen:
        return None
    if i < n and s[i] in "eE":
        i += 1
        eneg = False
        if i < n and s[i] in "+-":
            eneg = s[i] == "-"
            i += 1
        if i >= n or not ("0" <= s[i] <= "9"):
            return None
        e = 0
        while i < n and "0" <= s[i] <= "9":
            if e < 100000:
                e = e * 10 + (ord(s[i]) - 48)
            i += 1
        exp += -e if eneg else e
    if i != n:
        return None
    v = float(mant)
    if mant:
        if exp > 0:
            v = _scale_up(v, min(exp, 400))
        elif exp < 0:
            v = _scale_down(v, min(-exp, 400))
    return -v if neg else v


def parse_int(b):
    """bytes -> int32, or None. [+-]digits, must fit in int32."""
    s = b.decode("latin-1") if isinstance(b, bytes) else b
    i, n = 0, len(s)
    neg = False
    if i < n and s[i] in "+-":
        neg = s[i] == "-"
        i += 1
    if i >= n:
        return None
    v = 0
    while i < n:
        if not ("0" <= s[i] <= "9"):
            return None
        v = v * 10 + (ord(s[i]) - 48)
        if v > 2 ** 31:
            return None
        i += 1
    v = -v if neg else v
    if not -2 ** 31 <= v <= 2 ** 31 - 1:
        return None
    return v


def float_to_int(x):
    if x != x or x >= 2147483648.0 or x <= -2147483649.0:
        raise ValueFault(BAD_VALUE)
    return int(x)        # truncates toward zero, like the C cast


# libm, so that the reference VM gets the same bits as the C VM on the same machine
try:
    _libm = ctypes.CDLL(ctypes.util.find_library("m") or None)
    for _f in ("floor", "ceil", "sqrt", "exp", "log", "fmod"):
        getattr(_libm, _f).restype = ctypes.c_double
        getattr(_libm, _f).argtypes = [ctypes.c_double]
    _libm.fmod.argtypes = [ctypes.c_double, ctypes.c_double]
    _libm.pow.restype = ctypes.c_double
    _libm.pow.argtypes = [ctypes.c_double, ctypes.c_double]
except (OSError, AttributeError):          # pragma: no cover
    _libm = None


def _m(name, *args):
    if _libm is not None:
        return getattr(_libm, name)(*args)
    try:                                   # pragma: no cover
        return {"floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt, "exp": math.exp,
                "log": math.log, "fmod": math.fmod, "pow": math.pow}[name](*args) * 1.0
    except OverflowError:                  # pragma: no cover
        return math.inf
    except (ValueError, ZeroDivisionError):  # pragma: no cover
        return math.nan


def fdiv(a, b):
    if b == 0.0:
        if a != a or a == 0.0:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def fround(x):
    """Half away from zero, exactly (no a + 0.5 rounding error)."""
    if x != x or x in (math.inf, -math.inf):
        return x
    a = abs(x)
    t = _m("floor", a)
    if a - t >= 0.5:
        t += 1.0
    return math.copysign(t, x)


# ---- time -------------------------------------------------------------------------------
def days_from_civil(y, m, d):
    """Howard Hinnant's algorithm (integer, works for any int64-range year)."""
    y -= m <= 2
    era = y // 400                   # floor division (C: adjust the truncating division)
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z):
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d


def _days_in_month(y, m):
    if m == 2:
        return 29 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 28
    return 30 if m in (4, 6, 9, 11) else 31


def make_time(y, mo, d, h=0, mi=0, s=0):
    if not (1 <= y <= 9999 and 1 <= mo <= 12 and 1 <= d <= _days_in_month(y, mo)
            and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        raise ValueFault(BAD_VALUE)
    return Time(days_from_civil(y, mo, d) * DAY_MS + ((h * 60 + mi) * 60 + s) * 1000)


def split_time(ms):
    """-> (year, month, day, hour, minute, second, millisecond, weekday 0=Monday)"""
    days = ms // DAY_MS
    rem = ms - days * DAY_MS
    y, m, d = civil_from_days(days)
    wd = (days + 3) % 7               # 1970-01-01 was a Thursday
    return y, m, d, rem // 3600000, rem // 60000 % 60, rem // 1000 % 60, rem % 1000, wd


def fmt_time(ms):
    y, mo, d, h, mi, s, milli, _wd = split_time(ms)
    ys = ("-" if y < 0 else "") + str(abs(y)).rjust(4, "0")
    return "%s-%02d-%02dT%02d:%02d:%02d.%03dZ" % (ys, mo, d, h, mi, s, milli)


def parse_time(b):
    """ISO 8601: YYYY-MM-DD[(T| )HH:MM[:SS[.f{1,3}]]][Z|(+|-)HH:MM] -> ms, or None."""
    s = b.decode("latin-1") if isinstance(b, bytes) else b
    pos = [0]

    def num(k):
        i = pos[0]
        part = s[i:i + k]
        if len(part) != k or not all("0" <= c <= "9" for c in part):
            raise ValueError
        pos[0] = i + k
        return int(part)

    def lit(c):
        if s[pos[0]:pos[0] + 1] != c:
            raise ValueError
        pos[0] += 1

    try:
        y = num(4)
        lit("-")
        mo = num(2)
        lit("-")
        d = num(2)
        h = mi = sec = milli = 0
        if s[pos[0]:pos[0] + 1] in ("T", " ") and pos[0] < len(s):
            pos[0] += 1
            h = num(2)
            lit(":")
            mi = num(2)
            if s[pos[0]:pos[0] + 1] == ":":
                pos[0] += 1
                sec = num(2)
                if s[pos[0]:pos[0] + 1] == ".":
                    pos[0] += 1
                    k = 0
                    while k < 3 and pos[0] < len(s) and "0" <= s[pos[0]] <= "9":
                        milli = milli * 10 + int(s[pos[0]])
                        pos[0] += 1
                        k += 1
                    if k == 0:
                        raise ValueError
                    milli *= (1, 100, 10, 1)[k]
        offset = 0
        c = s[pos[0]:pos[0] + 1]
        if c == "Z":
            pos[0] += 1
        elif c in ("+", "-"):
            pos[0] += 1
            oh = num(2)
            lit(":")
            om = num(2)
            if oh > 23 or om > 59:
                raise ValueError
            offset = (oh * 60 + om) * 60000 * (1 if c == "+" else -1)
        if pos[0] != len(s):
            raise ValueError
        t = make_time(y, mo, d, h, mi, sec)
    except (ValueError, ValueFault):
        return None
    return t.ms + milli - offset


# ---- strings ----------------------------------------------------------------------------
def quote(b):
    """Canonical text of a string value in traces: "..." with escapes."""
    out = ['"']
    for c in b:
        if c == 0x22:
            out.append('\\"')
        elif c == 0x5C:
            out.append("\\\\")
        elif c == 0x0A:
            out.append("\\n")
        elif c == 0x09:
            out.append("\\t")
        elif 0x20 <= c < 0x7F:
            out.append(chr(c))
        else:
            out.append("\\x%02x" % c)
    out.append('"')
    return "".join(out)


def show(v):
    """Canonical text of any value (traces, debugger, str())."""
    t = type_of(v)
    if t == INT:
        return str(v)
    if t == FLOAT:
        return fmt_float(v)
    if t == STR:
        return quote(v.b)
    return fmt_time(v.ms)


def to_text(v):
    """str(x): like show(), but a string is itself (not quoted)."""
    t = type_of(v)
    if t == STR:
        return v.b
    return show(v).encode("ascii")


def unquote(text):
    """Inverse of quote() for scenario files / the debugger. Returns bytes or None."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return None
    body = text[1:-1].encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C:
            if i + 1 >= len(body):
                return None
            e = body[i + 1]
            if e == 0x78:                                   # \xNN
                h = body[i + 2:i + 4]
                if len(h) != 2 or any(ch not in b"0123456789abcdefABCDEF" for ch in h):
                    return None
                out.append(int(h, 16))
                i += 4
                continue
            m = {0x6E: 10, 0x74: 9, 0x22: 34, 0x5C: 92}.get(e)
            if m is None:
                return None
            out.append(m)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def parse_literal(t, text):
    """A value of type t from its text (scenario files, debugger). Raises ValueError."""
    text = text.strip()
    if t == INT:
        v = int(text, 0)
        return wrap(v)
    if t == FLOAT:
        v = parse_float(text.encode())
        if v is None:
            raise ValueError("not a float: %r" % text)
        return v
    if t == STR:
        b = unquote(text)
        if b is None:
            b = text.encode("utf-8")
        return Str(b)
    digits = text[1:] if text.startswith("-") else text
    if digits and all("0" <= c <= "9" for c in digits):
        return Time(int(text))
    ms = parse_time(text.encode())
    if ms is None:
        raise ValueError("not a time: %r (use YYYY-MM-DDTHH:MM:SSZ or ms)" % text)
    return Time(ms)


# ---- operators (same semantics as the VM opcodes) ----------------------------------------
def substr(b, start, count):
    n = len(b)
    if start < 0:
        start = 0
    if start > n:
        start = n
    if count < 0:
        count = 0
    end = start + count if count < n - start else n
    return b[start:end]


def find(b, sub):
    return b.find(sub)


def sat32(v):
    return max(-2 ** 31, min(2 ** 31 - 1, v))


def fcompare(op, a, b):
    return int({"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])


# ---- builtin functions --------------------------------------------------------------------
# id -> (name, param types, return type). The ids are part of the bytecode (BUILTIN n);
# c/flowscript.c has the same table. Overloads have their own id; the compiler picks one.
I, F, S, T = INT, FLOAT, STR, TIME
BUILTINS = [
    ("float", (I,), F), ("float", (S,), F), ("float", (T,), F),
    ("int", (F,), I), ("int", (S,), I),
    ("str", (I,), S), ("str", (F,), S), ("str", (T,), S),
    ("time", (S,), T), ("time", (I, I, I), T), ("time", (I, I, I, I, I, I), T),
    ("now", (), T), ("date", (T,), T),
    ("year", (T,), I), ("month", (T,), I), ("day", (T,), I), ("hour", (T,), I),
    ("minute", (T,), I), ("second", (T,), I), ("millisecond", (T,), I), ("weekday", (T,), I),
    ("len", (S,), I), ("substr", (S, I, I), S), ("find", (S, S), I),
    ("abs", (I,), I), ("abs", (F,), F),
    ("min", (I, I), I), ("max", (I, I), I), ("min", (F, F), F), ("max", (F, F), F),
    ("floor", (F,), F), ("ceil", (F,), F), ("round", (F,), F), ("sqrt", (F,), F),
    ("exp", (F,), F), ("log", (F,), F), ("pow", (F, F), F),
]
BUILTIN_NAMES = sorted(set(b[0] for b in BUILTINS))
IMPURE = {"now"}          # never folded at compile time
# conversions that are the identity (no code): int(int), float(float), str(str), time(time)
IDENTITY = {("int", I), ("float", F), ("str", S), ("time", T)}


def call_builtin(idx, args, now=None):
    """Evaluate builtin `idx`. Returns int/float/bytes/Time (bytes: a new string's text).

    Raises ValueFault for invalid conversions. `now` is a callable returning ms.
    """
    name, _ptypes, _ret = BUILTINS[idx]
    a = args
    if idx == 0:
        return float(a[0])
    if idx == 1:
        v = parse_float(a[0].b)
        if v is None:
            raise ValueFault(BAD_VALUE)
        return v
    if idx == 2:
        return a[0].ms / 1000.0
    if idx == 3:
        return float_to_int(a[0])
    if idx == 4:
        v = parse_int(a[0].b)
        if v is None:
            raise ValueFault(BAD_VALUE)
        return v
    if idx in (5, 6, 7):
        return to_text(a[0])
    if idx == 8:
        ms = parse_time(a[0].b)
        if ms is None:
            raise ValueFault(BAD_VALUE)
        return Time(ms)
    if idx in (9, 10):
        return make_time(*a)
    if idx == 11:
        return Time(now() if now else 0)
    if idx == 12:
        return Time(a[0].ms // DAY_MS * DAY_MS)
    if 13 <= idx <= 20:
        return wrap(split_time(a[0].ms)[idx - 13])
    if idx == 21:
        return len(a[0].b)
    if idx == 22:
        return substr(a[0].b, a[1], a[2])
    if idx == 23:
        return find(a[0].b, a[1].b)
    if idx == 24:
        return wrap(abs(a[0]))
    if idx == 25:
        return math.copysign(a[0], 1.0)
    if idx in (26, 28):
        return a[1] if a[1] < a[0] else a[0]
    if idx in (27, 29):
        return a[1] if a[1] > a[0] else a[0]
    if idx == 30:
        return _m("floor", a[0])
    if idx == 31:
        return _m("ceil", a[0])
    if idx == 32:
        return fround(a[0])
    if idx in (33, 34, 35):
        return _m(name, a[0])
    if idx == 36:
        return _m("pow", a[0], a[1])
    raise ValueFault(5)


def binary(op, a, b):
    """Evaluate a binary operator on two values of matching types (after promotion).

    Returns int/float/Time or bytes (a new string from +). Raises ValueFault / intops.DivZero.
    """
    from . import intops
    ta, tb = type_of(a), type_of(b)
    if ta == INT and tb == INT:
        return intops.BINARY[op](a, b)
    if ta == FLOAT and tb == FLOAT:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return fdiv(a, b)
        if op == "%":
            return _m("fmod", a, b)
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return fcompare(op, a, b)
    if ta == STR and tb == STR:
        if op == "+":
            return a.b + b.b
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return fcompare(op, a.b, b.b)
    if ta == TIME and tb == INT and op in ("+", "-"):
        return Time(a.ms + b if op == "+" else a.ms - b)
    if ta == INT and tb == TIME and op == "+":
        return Time(b.ms + a)
    if ta == TIME and tb == TIME:
        if op == "-":
            return sat32(wrap64(a.ms - b.ms))
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return fcompare(op, a.ms, b.ms)
    raise ValueFault(TYPE_MISMATCH)


def binary_type(op, ta, tb):
    """Result type of `a op b` after int->float promotion, or None if not allowed.

    Returns (result type, promote_left, promote_right)."""
    pl = pr = False
    if {ta, tb} == {INT, FLOAT}:
        pl, pr = ta == INT, tb == INT
        ta = tb = FLOAT
    cmp = op in ("==", "!=", "<", "<=", ">", ">=")
    if ta == tb == INT:
        return INT, pl, pr
    if ta == tb == FLOAT and op in ("+", "-", "*", "/", "%") + ("==", "!=", "<", "<=", ">", ">="):
        return (INT if cmp else FLOAT), pl, pr
    if ta == tb == STR and (op == "+" or cmp):
        return (INT if cmp else STR), pl, pr
    if ta == TIME and tb == INT and op in ("+", "-"):
        return TIME, pl, pr
    if ta == INT and tb == TIME and op == "+":
        return TIME, pl, pr
    if ta == tb == TIME and (op == "-" or cmp):
        return INT, pl, pr
    return None


def pack_value(v, string_index):
    """Global initial value in the image: u8 type + 8 bytes payload."""
    t = type_of(v)
    if t == INT:
        return struct.pack("<Bq", INT, v)
    if t == FLOAT:
        return struct.pack("<Bd", FLOAT, v)
    if t == STR:
        return struct.pack("<Bq", STR, string_index(v.b))
    return struct.pack("<Bq", TIME, v.ms)


def unpack_value(data, off, strings):
    t = data[off]
    if t == INT:
        return wrap(struct.unpack_from("<q", data, off + 1)[0])
    if t == FLOAT:
        return struct.unpack_from("<d", data, off + 1)[0]
    if t == STR:
        return Str(strings[struct.unpack_from("<q", data, off + 1)[0]].encode("utf-8", "surrogateescape"))
    if t == TIME:
        return Time(struct.unpack_from("<q", data, off + 1)[0])
    raise ValueError("bad global type %d" % t)
