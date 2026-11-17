"""Monitor mode (SPEC 10.5): what a program does, as it happens.

A recording (record.py) carries a run's inputs, and a viewer re-runs them: it needs the
device's exact image.  An observation stream carries the observations themselves (state
changes, changed variables, handlers run, log statements, raised events, extern calls,
faults), so a viewer only shows them: any compiler's build of the same source names
them, and nothing needs to run.

    frames = []
    machine = flowscript.Machine(program, externs=..., observe=frames.append)
    machine.start(); ...; machine.record_keyframe()      # now and then: a full picture

    mon = flowscript.Monitor(program, on_log=..., on_state=...)   # program: names only
    mon.feed(b"".join(frames))
    mon.state, mon.globals, mon["count"]
"""

import struct

from . import values as V
from .values import INT, FLOAT, STR, TIME, NONE_TYPE, Str, Time
from .record import Reader, frame, varint, zigzag, enc_value, dec_value

__all__ = ["Monitor", "MonitorError", "OBS_TAGS", "is_observation"]

OBS_VERSION = 1
OSTART, OKEYFRAME, OSTATE, OGLOBALS, OHANDLER, OLOG, OEMIT, OCALL, OFAULT = range(16, 25)
OBS_TAGS = {OSTART: "start", OKEYFRAME: "keyframe", OSTATE: "state", OGLOBALS: "globals",
            OHANDLER: "handler", OLOG: "log", OEMIT: "emit", OCALL: "call", OFAULT: "fault"}
RESULT_NONE, RESULT_REFUSED = 4, 5      # an OCALL's result byte besides the type codes


def text(b):
    b = b if isinstance(b, bytes) else b.encode("utf-8")
    return varint(len(b)) + b


def tagged(v):
    t = V.type_of(v)
    return bytes([t]) + enc_value(t, v)


def same(a, b):
    """Is a global's new value the old one?  Floats bit for bit, strings byte for byte."""
    ta, tb = V.type_of(a), V.type_of(b)
    if ta != tb:
        return False
    if ta == FLOAT:
        return struct.pack("<d", a) == struct.pack("<d", b)
    if ta == STR:
        return a.b == b.b
    if ta == TIME:
        return a.ms == b.ms
    return a == b


def is_observation(first_frame):
    """Is this frame (varint length + body) from an observation stream?"""
    r = Reader(first_frame)
    try:
        r.varint()
        return r.byte() in OBS_TAGS
    except ValueError:
        return False


class Observer:
    """Attached to a VM (``VM(observe=sink)``): turns what it does into frames for sink(bytes)."""

    def __init__(self, vm, sink):
        self.vm, self.sink = vm, sink
        self.dirty = set()

    def emit(self, tag, payload=b""):
        self.sink(frame(tag, payload))

    def header(self):
        vm, p = self.vm, self.vm.prog
        name = p.name if getattr(p, "name", None) else ""
        out = bytes([OBS_VERSION]) + text(name) + varint(p.version & 0xFFFF)
        out += struct.pack("<I", vm.image_hash) + varint(len(vm.globals))
        out += bytes(V.type_of(g) for g in vm.globals)
        out += text(p.states[vm.state] if vm.state is not None else "")
        return out + b"".join(enc_value(V.type_of(g), g) for g in vm.globals)

    def start(self):
        self.dirty.clear()
        self.emit(OSTART, self.header())

    def keyframe(self):
        self.dirty.clear()
        self.emit(OKEYFRAME, self.header())

    def changed(self, index, old, new):
        if not same(old, new):
            self.dirty.add(index)

    def flush(self):
        if self.dirty:
            g = self.vm.globals
            idx = sorted(self.dirty)
            self.dirty.clear()
            self.emit(OGLOBALS, varint(len(idx)) +
                      b"".join(varint(i) + enc_value(V.type_of(g[i]), g[i]) for i in idx))

    def state(self):
        self.flush()
        self.emit(OSTATE, text(self.vm.prog.states[self.vm.state]))

    def _where(self, h):
        p = self.vm.prog
        if h is None:
            return text("") + text("")
        return text(p.states[h[0]] if h[0] is not None else "") + text(p.events[h[1]][0])

    def handler(self, h, args):
        self.flush()
        self.emit(OHANDLER, self._where(h) + varint(len(args)) + b"".join(tagged(a) for a in args))

    def log(self, level, template, values):
        self.emit(OLOG, bytes([level]) + text(template) + varint(len(values)) +
                  b"".join(tagged(v) for v in values))

    def raised(self, event, args):
        self.emit(OEMIT, text(self.vm.prog.events[event][0]) + varint(len(args)) +
                  b"".join(tagged(a) for a in args))

    def call(self, ext, args, result=None, refused=None, refused_value=None):
        """result: the accepted value (None: the extern returns nothing); refused: a fault code."""
        name, _argc, _pt, ret = self.vm.prog.externs[ext]
        out = text(name) + varint(len(args)) + b"".join(tagged(a) for a in args)
        if refused is not None:
            out += bytes([RESULT_REFUSED]) + varint(refused)
            if refused in (V.OUT_OF_STRINGS, V.STRING_TOO_LONG) and ret == STR:
                b = (refused_value.b if isinstance(refused_value, Str) else
                     refused_value.encode("utf-8") if isinstance(refused_value, str) else bytes(refused_value))
                out += text(b)
        elif ret == NONE_TYPE:
            out += bytes([RESULT_NONE])
        else:
            out += tagged(result)
        self.emit(OCALL, out)

    def fault(self, code, pc):
        self.emit(OFAULT, varint(code) + self._where(self.vm.handler) + varint(zigzag(pc)))


# ---- reading ------------------------------------------------------------------------------------
class MonitorError(ValueError):
    """A damaged or unsupported observation frame."""


def _text(r):
    return r.take(r.varint())


def _value(t, r):
    """A value of type t as the VM holds it (a str as values.Str)."""
    v = dec_value(t, r)
    return Str(v) if t == STR else v


def _tagged(r):
    t = r.byte()
    if t > TIME:
        raise MonitorError("bad value type %d" % t)
    return _value(t, r)


class Monitor:
    """Follow an observation stream (SPEC 10.5).  Nothing runs: the stream says what happened.

        mon = Monitor(program, on_state=..., on_log=..., on_event=..., on_call=..., on_fault=...,
                      on_handler=..., on_globals=...)
        mon.feed(data)            # frames as they arrive, in any chunks
        mon.state, mon.globals, mon["name"], mon.joined

    program (optional) gives the variables' names: globals are numbered in declaration order
    (SPEC 10.5), so any compiler's build of the same source names them.  ``mismatch`` says
    why the names were not used (the stream's variables don't fit the program).  Without a
    program the names are "global[i]".  Callbacks take Python values (str, int, float,
    datetime), like Replay's, or with raw=True the VM's own (values.Str, values.Time), as in
    ``raw``, the current values.
    """

    def __init__(self, program=None, *, on_state=None, on_globals=None, on_handler=None, on_log=None,
                 on_event=None, on_call=None, on_fault=None, on_start=None, raw=False):
        from .api import _to_py
        self.program = program
        self._py = (lambda v: v) if raw else _to_py
        self.cb = dict(state=on_state, globals=on_globals, handler=on_handler, log=on_log,
                       event=on_event, call=on_call, fault=on_fault, start=on_start)
        self.buf = b""
        self.joined = False
        self.skipped = 0
        self.name = None
        self.version = None
        self.image_hash = None
        self.types = []
        self.raw = []
        self.state = None
        self.names = []
        self.mismatch = None

    # ---- input ----
    def feed(self, data):
        self.buf += bytes(data)
        r = Reader(self.buf)
        while r.pos < len(self.buf):
            start = r.pos
            try:
                n = r.varint()
                body = r.take(n)
            except ValueError:
                r.pos = start
                break
            if body:
                self.apply(body[0], body[1:])
        self.buf = self.buf[r.pos:]

    def apply(self, tag, payload):
        _to_py = self._py
        r = Reader(payload)
        try:
            if tag in (OSTART, OKEYFRAME):
                self._header(r, tag)
                return
            if not self.joined:
                self.skipped += 1
                return
            cb = self.cb
            if tag == OSTATE:
                self.state = _text(r).decode("utf-8", "replace")
                if cb["state"]:
                    cb["state"](self.state)
            elif tag == OGLOBALS:
                changes = {}
                for _ in range(r.varint()):
                    i = r.varint()
                    if i >= len(self.types):
                        raise MonitorError("global %d: the stream declared %d" % (i, len(self.types)))
                    self.raw[i] = _value(self.types[i], r)
                    changes[self.names[i]] = _to_py(self.raw[i])
                if cb["globals"]:
                    cb["globals"](changes)
            elif tag == OHANDLER:
                st, ev = _text(r).decode("utf-8", "replace"), _text(r).decode("utf-8", "replace")
                args = [_tagged(r) for _ in range(r.varint())]
                if cb["handler"]:
                    cb["handler"](st, ev, [_to_py(a) for a in args])
            elif tag == OLOG:
                level = r.byte()
                template = _text(r)
                values = [_tagged(r) for _ in range(r.varint())]
                if cb["log"]:
                    message = V.format_log(template, values).decode("utf-8", "surrogateescape")
                    cb["log"](V.LOG_LEVELS[level] if level < 4 else str(level), message,
                              template.decode("utf-8", "replace"), [_to_py(v) for v in values])
            elif tag == OEMIT:
                name = _text(r).decode("utf-8", "replace")
                args = [_tagged(r) for _ in range(r.varint())]
                if cb["event"]:
                    cb["event"](name, [_to_py(a) for a in args])
            elif tag == OCALL:
                name = _text(r).decode("utf-8", "replace")
                args = [_tagged(r) for _ in range(r.varint())]
                rt = r.byte()
                if rt == RESULT_NONE:
                    result = None
                elif rt == RESULT_REFUSED:
                    code = r.varint()
                    result = Refused(code, Str(_text(r)) if r.pos < len(payload) else None)
                elif rt <= TIME:
                    result = _value(rt, r)
                else:
                    raise MonitorError("bad call result type %d" % rt)
                if cb["call"]:
                    cb["call"](name, [_to_py(a) for a in args],
                               result if result is None or isinstance(result, Refused) else _to_py(result))
            elif tag == OFAULT:
                code = r.varint()
                st, ev = _text(r).decode("utf-8", "replace"), _text(r).decode("utf-8", "replace")
                pc = r.svarint()
                if cb["fault"]:
                    cb["fault"](code, st or None, ev or None, pc)
            else:
                raise MonitorError("frame tag %d is not an observation (SPEC 10.5)" % tag)
        except ValueError as e:
            if isinstance(e, MonitorError):
                raise
            raise MonitorError("damaged %s frame: %s" % (OBS_TAGS.get(tag, tag), e))

    def _header(self, r, tag):
        version = r.byte()
        if version != OBS_VERSION:
            raise MonitorError("observation stream version %d (this reader knows %d)" % (version, OBS_VERSION))
        self.name = _text(r).decode("utf-8", "replace") or None
        self.version = r.varint()
        self.image_hash = r.unpack("I")
        n = r.varint()
        types = list(r.take(n))
        if any(t > TIME for t in types):
            raise MonitorError("bad global type")
        state = _text(r).decode("utf-8", "replace") or None
        raw = [_value(t, r) for t in types]
        self.types, self.raw, self.state = types, raw, state
        self.names, self.mismatch = self._names(types)
        self.joined = True
        if self.cb["start"]:
            self.cb["start"](tag == OSTART)

    def _names(self, types):
        fallback = ["global[%d]" % i for i in range(len(types))]
        p = self.program
        if p is None:
            return fallback, None
        want = [V.type_of(g) for g in p.globals]
        pname = getattr(p, "name", None)
        if pname and self.name and pname != self.name:
            return fallback, "the device runs program '%s', not '%s'" % (self.name, pname)
        if want != types:
            return fallback, ("the device's program has %d variables of other types than this "
                              "source's %d: names not shown" % (len(types), len(want)))
        names = list(getattr(p, "global_names", None) or [])
        return [names[i] if i < len(names) and names[i] else fallback[i] for i in range(len(types))], None

    # ---- the current picture ----
    @property
    def globals(self):
        from .api import _to_py
        return {n: _to_py(v) for n, v in zip(self.names, self.raw)}

    def __getitem__(self, name):
        from .api import _to_py
        if name not in self.names:
            raise KeyError("no variable %r" % name)
        return _to_py(self.raw[self.names.index(name)])


class Refused:
    """An extern result the VM refused (the call faulted with ``code``)."""
    __slots__ = ("code", "value")

    def __init__(self, code, value=None):
        self.code, self.value = code, value

    def __repr__(self):
        return "Refused(%d%s)" % (self.code, ", %r" % self.value if self.value is not None else "")
