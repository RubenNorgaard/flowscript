"""Bytecode definitions, .fsb file format (encode/decode) and disassembler.

See SPEC.md section 8 for the normative description.
"""

import struct

MAGIC = b"FSB1"
FORMAT_VERSION = 1
FLAG_DEBUG = 1
NONE16 = 0xFFFF

EV_BOOT, EV_ENTER, EV_EXIT = 0, 1, 2
BUILTIN_EVENTS = ["boot", "enter", "exit"]

# name -> (opcode, operand kind)   operand kinds: None, i32, i8, u8, u16
OPCODES = {
    "NOP": (0x00, None), "PUSH": (0x01, "i32"), "PUSH8": (0x02, "i8"), "PUSHS": (0x03, "u16"),
    "POP": (0x04, None), "DUP": (0x05, None),
    "LOADG": (0x10, "u16"), "STOREG": (0x11, "u16"), "LOADL": (0x12, "u8"), "STOREL": (0x13, "u8"),
    "ADD": (0x20, None), "SUB": (0x21, None), "MUL": (0x22, None), "DIV": (0x23, None),
    "MOD": (0x24, None), "NEG": (0x25, None), "BAND": (0x26, None), "BOR": (0x27, None),
    "BXOR": (0x28, None), "BNOT": (0x29, None), "SHL": (0x2A, None), "SHR": (0x2B, None),
    "EQ": (0x30, None), "NE": (0x31, None), "LT": (0x32, None), "LE": (0x33, None),
    "GT": (0x34, None), "GE": (0x35, None), "NOT": (0x36, None), "BOOL": (0x37, None),
    "JMP": (0x40, "u16"), "JZ": (0x41, "u16"), "JNZ": (0x42, "u16"),
    "CALL": (0x50, "u16"), "RET": (0x51, None), "RET0": (0x52, None), "XCALL": (0x53, "u16"),
    "GOTO": (0x60, "u16"), "EMIT": (0x61, "u16"), "TSTART": (0x62, "u16"),
    "TEVERY": (0x63, "u16"), "TSTOP": (0x64, "u16"),
}
BY_CODE = {code: (name, kind) for name, (code, kind) in OPCODES.items()}
OPERAND_SIZE = {None: 0, "i8": 1, "u8": 1, "u16": 2, "i32": 4}
OPERAND_FMT = {"i8": "<b", "u8": "<B", "u16": "<H", "i32": "<i"}

FAULTS = {
    1: "stack overflow", 2: "stack underflow", 3: "division by zero",
    4: "instruction budget exceeded", 5: "bad opcode", 6: "call depth exceeded",
    7: "event queue full", 8: "unbound extern", 9: "dispatch chain limit (script never idle)",
}


class Program:
    """In-memory representation of a compiled program (mirrors the .fsb file)."""

    def __init__(self):
        self.name = None
        self.version = 0
        self.strings = []      # list[str]
        self.globals = []      # initial values
        self.externs = []      # (name, argc)
        self.events = []       # (name, argc)
        self.states = []       # name
        self.timers = []       # event index
        self.funcs = []        # (name, argc, nlocals, offset)
        self.handlers = []     # (state|None, event, argc, nlocals, offset)
        self.code = b""
        # debug info (optional)
        self.lines = []        # (pc, line, node)
        self.global_names = []

    # -- helpers -------------------------------------------------------------
    def event_index(self, name):
        for i, (n, _) in enumerate(self.events):
            if n == name:
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
    def to_bytes(self, debug=False):
        strings = list(self.strings)

        def sidx(s):
            if s is None:
                return NONE16
            if s not in strings:
                strings.append(s)
            return strings.index(s)

        name_idx = sidx(self.name)
        ext = b"".join(struct.pack("<HBB", sidx(n), a, 0) for n, a in self.externs)
        evs = b"".join(struct.pack("<HBB", sidx(n), a, 0) for n, a in self.events)
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
            b = s.encode("utf-8")
            strs += struct.pack("<H", len(b)) + b + b"\x00"
        glb = b"".join(struct.pack("<i", v) for v in self.globals)
        header = MAGIC + struct.pack(
            "<HHHHHHHHHHHHI", FORMAT_VERSION, FLAG_DEBUG if debug else 0, name_idx,
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
            p.strings.append(data[off + 2:off + 2 + ln].decode("utf-8"))
            off += 3 + ln
        for _ in range(ng):
            p.globals.append(struct.unpack_from("<i", data, off)[0])
            off += 4
        for _ in range(nx):
            n, a, _r = struct.unpack_from("<HBB", data, off)
            p.externs.append((p.strings[n], a))
            off += 4
        for _ in range(ne):
            n, a, _r = struct.unpack_from("<HBB", data, off)
            p.events.append((p.strings[n], a))
            off += 4
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
        if name == "PUSHS":
            return repr(prog.strings[val])
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
    for i, (n, a) in enumerate(prog.externs):
        lines.append("; extern %d: %s/%d" % (i, n, a))
    for i, (n, a) in enumerate(prog.events):
        lines.append("; event  %d: %s/%d" % (i, n, a))
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
