"""Bytecode definitions, .fsb file format (encode/decode) and disassembler.

See SPEC.md section 8 for the normative description.
"""

import struct

from .values import pack_value, unpack_value, TYPE_NAMES, NONE_TYPE, show, BUILTINS

MAGIC = b"FSB1"
FORMAT_VERSION = 4
FLAG_DEBUG = 1
# Features the program needs from the VM.  A C VM built without one (FS_NO_FLOAT,
# FS_NO_TIME, FS_NO_STR) refuses images that set its flag.
FLAG_FLOAT = 2       # float values
FLAG_TIME = 4        # time values
FLAG_STR = 8         # strings made at run time (string slots); literals need no flag
FLAG_TEXT = 16       # text conversion of floats/times: str(float), str(time), float(s), time(s)
FEATURES = [(FLAG_FLOAT, "float", "FS_NO_FLOAT"), (FLAG_TIME, "time", "FS_NO_TIME"),
            (FLAG_STR, "run-time strings", "FS_NO_STR"),
            (FLAG_TEXT, "float/time text conversion", "FS_NO_TEXT")]
TEXT_BUILTINS = (1, 6, 7, 8)   # float(str), str(float), str(time), time(str)
NONE16 = 0xFFFF

EV_BOOT, EV_ENTER, EV_EXIT = 0, 1, 2
BUILTIN_EVENTS = ["boot", "enter", "exit"]

# name -> (opcode, operand kind)   operand kinds: None, i32, i8, u8, u16, f64, i64
OPCODES = {
    "NOP": (0x00, None), "PUSH": (0x01, "i32"), "PUSH8": (0x02, "i8"), "PUSHS": (0x03, "u16"),
    "POP": (0x04, None), "DUP": (0x05, None), "PUSHF": (0x06, "f64"), "PUSHT": (0x07, "i64"),
    "LOADG": (0x10, "u16"), "STOREG": (0x11, "u16"), "LOADL": (0x12, "u8"), "STOREL": (0x13, "u8"),
    "ADD": (0x20, None), "SUB": (0x21, None), "MUL": (0x22, None), "DIV": (0x23, None),
    "MOD": (0x24, None), "NEG": (0x25, None), "BAND": (0x26, None), "BOR": (0x27, None),
    "BXOR": (0x28, None), "BNOT": (0x29, None), "SHL": (0x2A, None), "SHR": (0x2B, None),
    "CONCAT": (0x2C, None),
    "EQ": (0x30, None), "NE": (0x31, None), "LT": (0x32, None), "LE": (0x33, None),
    "GT": (0x34, None), "GE": (0x35, None), "NOT": (0x36, None), "BOOL": (0x37, None),
    "JMP": (0x40, "u16"), "JZ": (0x41, "u16"), "JNZ": (0x42, "u16"),
    "CALL": (0x50, "u16"), "RET": (0x51, None), "RET0": (0x52, None), "XCALL": (0x53, "u16"),
    "BUILTIN": (0x54, "u8"), "RETN": (0x55, "u8"),
    "GOTO": (0x60, "u16"), "EMIT": (0x61, "u16"), "TSTART": (0x62, "u16"),
    "TEVERY": (0x63, "u16"), "TSTOP": (0x64, "u16"),
    # log statements: u16 = the message template (a string); its '{}' slots say how many values
    "LOGD": (0x65, "u16"), "LOGI": (0x66, "u16"), "LOGW": (0x67, "u16"), "LOGE": (0x68, "u16"),
}
BY_CODE = {code: (name, kind) for name, (code, kind) in OPCODES.items()}
OPERAND_SIZE = {None: 0, "i8": 1, "u8": 1, "u16": 2, "i32": 4, "f64": 8, "i64": 8}
OPERAND_FMT = {"i8": "<b", "u8": "<B", "u16": "<H", "i32": "<i", "f64": "<d", "i64": "<q"}

FAULTS = {
    1: "stack overflow", 2: "stack underflow", 3: "division by zero",
    4: "instruction budget exceeded", 5: "bad opcode", 6: "call depth exceeded",
    7: "event queue full", 8: "unbound extern", 9: "dispatch chain limit (script never idle)",
    10: "type mismatch", 11: "out of string memory", 12: "string too long", 13: "invalid value",
}


def pack_types(types):
    """Parameter types, 2 bits each (int 0, float 1, str 2, time 3), first parameter lowest."""
    v = 0
    for i, t in enumerate(types):
        v |= t << (2 * i)
    return v


def unpack_types(v, n):
    return tuple((v >> (2 * i)) & 3 for i in range(n))


class Program:
    """In-memory representation of a compiled program (mirrors the .fsb file)."""

    def __init__(self):
        self.name = None
        self.version = 0
        self.strings = []      # list[str] (non-UTF-8 bytes kept with surrogateescape)
        self.globals = []      # initial values (int, float, values.Str, values.Time)
        self.externs = []      # (name, argc, param types, return type or NONE_TYPE)
        self.events = []       # (name, argc, param types)
        self.states = []       # name
        self.timers = []       # event index
        self.funcs = []        # (name, argc, nlocals, offset)
        self.handlers = []     # (state|None, event, argc, nlocals, offset)
        self.code = b""
        # debug info (optional)
        self.lines = []        # (pc, line, node)
        self.global_names = []
        # set by flowscript.compile(); None/empty for a program loaded from bytecode
        self.source = None
        self.ast = None
        self.debug_info = None
        self.warnings = []

    # -- helpers -------------------------------------------------------------
    def event_index(self, name):
        for i, ev in enumerate(self.events):
            if ev[0] == name:
                return i
        return -1

    def state_index(self, name):
        try:
            return self.states.index(name)
        except ValueError:
            return -1

    def find_handler(self, state, event):
        glob = None
        for h in self.handlers:
            if h[1] != event:
                continue
            if h[0] == state and state is not None:
                return h
            if h[0] is None and glob is None:
                glob = h
        return glob

    # -- serialisation ---------------------------------------------------------
    def save(self, path, debug=False):
        """Write the .fsb image (debug=True adds source lines and global names)."""
        with open(path, "wb") as f:
            f.write(self.to_bytes(debug))

    def to_bytes(self, debug=False):
        strings = list(self.strings)

        def sidx(s):
            if s is None:
                return NONE16
            if s not in strings:
                strings.append(s)
            return strings.index(s)

        name_idx = sidx(self.name)
        ext = b"".join(struct.pack("<HBBI", sidx(n), a, r, pack_types(ts)) for n, a, ts, r in self.externs)
        evs = b"".join(struct.pack("<HBBH", sidx(n), a, 0, pack_types(ts)) for n, a, ts in self.events)
        glb = b"".join(pack_value(v, lambda b: sidx(b.decode("utf-8", "surrogateescape"))) for v in self.globals)
        sts = b"".join(struct.pack("<H", sidx(n)) for n in self.states)
        tms = b"".join(struct.pack("<H", e) for e in self.timers)
        fns = b"".join(struct.pack("<HBBH", sidx(n), a, l, o) for n, a, l, o in self.funcs)
        hds = b"".join(struct.pack("<HHBBH", NONE16 if s is None else s, e, a, l, o)
                       for s, e, a, l, o in self.handlers)
        dbg = b""
        if debug:
            dbg = struct.pack("<H", len(self.lines))
            dbg += b"".join(struct.pack("<HHH", pc, ln & 0xFFFF, nd & 0xFFFF) for pc, ln, nd in self.lines)
            names = self.global_names or [""] * len(self.globals)
            dbg += b"".join(struct.pack("<H", sidx(n)) for n in names)
        strs = b""
        for s in strings:
            b = s.encode("utf-8", "surrogateescape")
            strs += struct.pack("<H", len(b)) + b + b"\x00"
        header = MAGIC + struct.pack(
            "<HHHHHHHHHHHHI", FORMAT_VERSION, (FLAG_DEBUG if debug else 0) | features(self), name_idx,
            self.version & 0xFFFF, len(strings), len(self.globals), len(self.externs),
            len(self.events), len(self.states), len(self.timers), len(self.funcs),
            len(self.handlers), len(self.code))
        assert len(header) == 32
        return header + strs + glb + ext + evs + sts + tms + fns + hds + self.code + dbg

    @classmethod
    def from_bytes(cls, data):
        try:
            return cls._from_bytes(data)
        except ValueError:
            raise
        except (IndexError, struct.error, UnicodeDecodeError) as e:
            raise ValueError("corrupt bytecode image: %s" % e)

    @classmethod
    def _from_bytes(cls, data):
        if data[:4] != MAGIC:
            raise ValueError("not a FlowScript bytecode file")
        (ver, flags, name_idx, pver, ns, ng, nx, ne, nst, nt, nf, nh,
         csize) = struct.unpack_from("<HHHHHHHHHHHHI", data, 4)
        if ver != FORMAT_VERSION:
            raise ValueError("unsupported bytecode version %d" % ver)
        p = cls()
        off = 32
        for _ in range(ns):
            (ln,) = struct.unpack_from("<H", data, off)
            if len(data) < off + 3 + ln:
                raise ValueError("corrupt bytecode image: string table truncated")
            p.strings.append(data[off + 2:off + 2 + ln].decode("utf-8", "surrogateescape"))
            off += 3 + ln
        for _ in range(ng):
            p.globals.append(unpack_value(data, off, p.strings))
            off += 9
        for _ in range(nx):
            n, a, r, ts = struct.unpack_from("<HBBI", data, off)
            p.externs.append((p.strings[n], a, unpack_types(ts, a), r))
            off += 8
        for _ in range(ne):
            n, a, _r, ts = struct.unpack_from("<HBBH", data, off)
            p.events.append((p.strings[n], a, unpack_types(ts, a)))
            off += 6
        for _ in range(nst):
            p.states.append(p.strings[struct.unpack_from("<H", data, off)[0]])
            off += 2
        for _ in range(nt):
            p.timers.append(struct.unpack_from("<H", data, off)[0])
            off += 2
        for _ in range(nf):
            n, a, l, o = struct.unpack_from("<HBBH", data, off)
            p.funcs.append((p.strings[n], a, l, o))
            off += 6
        for _ in range(nh):
            s, e, a, l, o = struct.unpack_from("<HHBBH", data, off)
            p.handlers.append((None if s == NONE16 else s, e, a, l, o))
            off += 8
        p.code = bytes(data[off:off + csize])
        off += csize
        if flags & FLAG_DEBUG:
            (nl,) = struct.unpack_from("<H", data, off)
            off += 2
            for _ in range(nl):
                p.lines.append(struct.unpack_from("<HHH", data, off))
                off += 6
            for _ in range(ng):
                p.global_names.append(p.strings[struct.unpack_from("<H", data, off)[0]])
                off += 2
        p.name = None if name_idx == NONE16 else p.strings[name_idx]
        p.version = pver
        return p


def features(prog):
    """FLAG_FLOAT | FLAG_TIME | FLAG_STR | FLAG_TEXT bits for what `prog` can ever use.

    Every value of those kinds starts at a typed declaration (global, extern, event),
    a typed constant push, CONCAT or a built-in, so scanning them is exact.
    """
    from .values import FLOAT, TIME, STR, type_of
    need = set()
    for v in prog.globals:
        need.add(type_of(v))
    for _n, _a, ts, r in prog.externs:
        need.update(ts)
        if r == STR:
            need.add("str-result")
        need.add(r)
    for _n, _a, ts in prog.events:
        need.update(ts)
        if STR in ts:
            need.add("str-result")          # the host's strings are copied into slots
    pc = 0
    while pc < len(prog.code):
        name, val, size = decode(prog.code, pc)
        if name == "PUSHF":
            need.add(FLOAT)
        elif name == "PUSHT":
            need.add(TIME)
        elif name == "CONCAT":
            need.add("str-result")
        elif name == "BUILTIN" and 0 <= val < len(BUILTINS):
            _bn, ps, r = BUILTINS[val]
            need.update(ps)
            need.add(r)
            if r == STR:
                need.add("str-result")
            if val in TEXT_BUILTINS:
                need.add("text")
        pc += size
    return ((FLAG_FLOAT if FLOAT in need else 0) | (FLAG_TIME if TIME in need else 0) |
            (FLAG_STR if "str-result" in need else 0) | (FLAG_TEXT if "text" in need else 0))


def decode(code, pc):
    """Decode one instruction. Returns (name, operand, size)."""
    op = code[pc]
    if op not in BY_CODE:
        return ("???", op, 1)
    name, kind = BY_CODE[op]
    if kind is None:
        return (name, None, 1)
    (val,) = struct.unpack_from(OPERAND_FMT[kind], code, pc + 1)
    return (name, val, 1 + OPERAND_SIZE[kind])


def operand_comment(prog, name, val, local_names=None):
    try:
        if name in ("PUSHS", "LOGD", "LOGI", "LOGW", "LOGE"):
            return show_str(prog.strings[val])
        if name == "PUSHT":
            from .values import Time
            return show(Time(val))
        if name == "BUILTIN":
            b = BUILTINS[val]
            return "%s(%s) -> %s" % (b[0], ", ".join(TYPE_NAMES[t] for t in b[1]), TYPE_NAMES[b[2]])
        if name in ("LOADG", "STOREG"):
            return prog.global_names[val] if prog.global_names else ""
        if name in ("LOADL", "STOREL") and local_names:
            return local_names[val] if val < len(local_names) else ""
        if name == "CALL":
            return prog.funcs[val][0]
        if name == "XCALL":
            return prog.externs[val][0]
        if name == "GOTO":
            return prog.states[val]
        if name == "EMIT":
            return prog.events[val][0]
        if name in ("TSTART", "TEVERY", "TSTOP"):
            return prog.events[prog.timers[val]][0]
    except (IndexError, TypeError):
        pass
    return ""


def show_str(s):
    from .values import Str
    return show(Str(s.encode("utf-8", "surrogateescape")))


def sig(types, ret=None):
    out = "(%s)" % ", ".join(TYPE_NAMES[t] for t in types)
    if ret is not None and ret != NONE_TYPE:
        out += " -> " + TYPE_NAMES[ret]
    return out


def code_labels(prog):
    """pc -> label for handler/function entry points."""
    labels = {}
    for name, _a, _l, off in prog.funcs:
        labels[off] = "func %s" % name
    for st, ev, _a, _l, off in prog.handlers:
        evn = prog.events[ev][0]
        labels[off] = ("on %s" % evn) if st is None else ("%s: on %s" % (prog.states[st], evn))
    return labels


def disassemble(prog, local_names=None):
    """Returns a list of dicts: pc, size, bytes(hex), op, arg, comment, label."""
    out = []
    labels = code_labels(prog)
    pc = 0
    code = prog.code
    while pc < len(code):
        name, val, size = decode(code, pc)
        out.append({
            "pc": pc, "size": size, "hex": code[pc:pc + size].hex(" "),
            "op": name, "arg": val,
            "comment": operand_comment(prog, name, val, (local_names or {}).get(pc)),
            "label": labels.get(pc),
        })
        pc += size
    return out


def disassemble_text(prog):
    lines = []
    if prog.name:
        lines.append("; flow %s version %d" % (prog.name, prog.version))
    lines.append("; %d bytes code, %d globals, %d externs, %d events, %d states, %d timers"
                 % (len(prog.code), len(prog.globals), len(prog.externs), len(prog.events),
                    len(prog.states), len(prog.timers)))
    for i, (n, _a, ts, r) in enumerate(prog.externs):
        lines.append("; extern %d: %s%s" % (i, n, sig(ts, r)))
    for i, (n, _a, ts) in enumerate(prog.events):
        lines.append("; event  %d: %s%s" % (i, n, sig(ts)))
    for i, v in enumerate(prog.globals):
        name = prog.global_names[i] if i < len(prog.global_names) else ""
        lines.append("; global %d: %s = %s" % (i, name, show(v)))
    for i, n in enumerate(prog.states):
        lines.append("; state  %d: %s" % (i, n))
    for ins in disassemble(prog):
        if ins["label"]:
            lines.append("")
            lines.append("%s:" % ins["label"])
        arg = "" if ins["arg"] is None else str(ins["arg"])
        cmt = ("  ; " + ins["comment"]) if ins["comment"] else ""
        lines.append("  %04x  %-14s %-7s %s%s" % (ins["pc"], ins["hex"], ins["op"], arg, cmt))
    return "\n".join(lines) + "\n"
