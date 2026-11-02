"""Snapshots, recordings and replay (SPEC section 10).

Snapshot: the VM's state while idle (current state, globals, timers) as compact bytes, tied
to the image by a CRC-32.  ``VM.snapshot()`` / ``VM.restore(data)``; ``fs_snapshot`` /
``fs_restore`` in C produce and accept the same bytes.

Recording: the VM is deterministic, so a run is reproduced by its *inputs* alone: events
the host posts, ticks, host-timer expiries, values returned by externs, clock reads, and
how many steps ran between them.  A recording VM hands each input to a sink as one frame:

    frame   = varint length, then length bytes: tag, payload

A ``Replay`` re-executes a recording against the same image and gets everything back:
every state, variable, log line, emitted event and fault.  Keyframes (a snapshot plus the
session header) let a viewer join a stream that is already running.

Encodings: varint = LEB128 (7 bits a byte, low first); zigzag maps signed to unsigned
(0, -1, 1, -2 ... -> 0, 1, 2, 3 ...).  In frames a value is encoded by the type the program
declares for it: int zigzag varint, float 8 bytes (IEEE, little endian), time zigzag varint,
str varint length + bytes.
"""

import struct
import zlib

from . import values as V
from .values import INT, FLOAT, STR, TIME, NONE_TYPE, Str, Time

__all__ = ["image_hash", "Replay", "read_frames", "SNAPSHOT_MAGIC"]

SNAPSHOT_MAGIC = b"FSNP"
SNAPSHOT_VERSION = 1
STR_EMPTY, STR_IMAGE, STR_RUNTIME = 0, 1, 2      # how a str global is stored in a snapshot

# frame tags
START, KEYFRAME, POST, POSTFAIL, TICK, EXPIRED, STEPS, RESULT, XFAULT, CLOCK, RESET, SETG = range(1, 13)
TAG_NAMES = {START: "start", KEYFRAME: "keyframe", POST: "post", POSTFAIL: "postfail", TICK: "tick",
             EXPIRED: "expired", STEPS: "steps", RESULT: "result", XFAULT: "xfault", CLOCK: "clock",
             RESET: "reset", SETG: "setg"}


def image_hash(image):
    """CRC-32 (IEEE, as zlib.crc32) of the exact image bytes."""
    return zlib.crc32(bytes(image)) & 0xFFFFFFFF


# ---- primitive encodings ------------------------------------------------------------------
def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(v):
    return (v << 1) ^ (v >> 63) if v < 0 else v << 1


def unzigzag(u):
    return (u >> 1) ^ -(u & 1)


class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def byte(self):
        if self.pos >= len(self.data):
            raise ValueError("truncated")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n):
        if self.pos + n > len(self.data):
            raise ValueError("truncated")
        b = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return b

    def varint(self):
        shift = result = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise ValueError("varint too long")

    def svarint(self):
        return unzigzag(self.varint())

    def unpack(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]


def enc_value(t, v):
    """A value of declared type t, without a type tag (frames)."""
    if t == INT:
        return varint(zigzag(V.wrap(int(v))))
    if t == FLOAT:
        return struct.pack("<d", float(v))
    if t == TIME:
        return varint(zigzag(v.ms))
    b = v.b if isinstance(v, Str) else v
    return varint(len(b)) + b


def dec_value(t, r):
    """-> int, float, Time, or bytes for a str."""
    if t == INT:
        return V.wrap(r.svarint())
    if t == FLOAT:
        return r.unpack("d")
    if t == TIME:
        return Time(V.wrap64(r.svarint()))
    return r.take(r.varint())


def frame(tag, payload=b""):
    body = bytes([tag]) + payload
    return varint(len(body)) + body


def read_frames(data):
    """Split concatenated frames -> [(tag, payload bytes)]; a cut-off last frame is left out."""
    r = Reader(data)
    out = []
    while r.pos < len(data):
        start = r.pos
        try:
            n = r.varint()
            body = r.take(n)
        except ValueError:
            r.pos = start
            break
        if not body:
            raise ValueError("empty frame")
        out.append((body[0], body[1:]))
    return out


# ---- snapshots ------------------------------------------------------------------------------
def snapshot(vm):
    """The VM's state while idle -> bytes (fs_snapshot)."""
    if vm.handler is not None or vm.has_work():
        raise RuntimeError("snapshot: the VM is busy (take it when idle, after run())")
    out = bytearray(SNAPSHOT_MAGIC)
    out += struct.pack("<HIhH", SNAPSHOT_VERSION, vm.image_hash, -1 if vm.state is None else vm.state,
                       len(vm.globals))
    for g in vm.globals:
        t = V.type_of(g)
        out.append(t)
        if t == INT:
            out += struct.pack("<i", g)
        elif t == FLOAT:
            out += struct.pack("<d", g)
        elif t == TIME:
            out += struct.pack("<q", g.ms)
        elif not g.b:
            out.append(STR_EMPTY)
        elif g.slot is None:                         # a string of the image: by index
            out.append(STR_IMAGE)
            out += struct.pack("<H", vm.string_table().index(g.b.decode("utf-8", "surrogateescape")))
        else:                                        # made at run time: its bytes
            out.append(STR_RUNTIME)
            out += struct.pack("<H", len(g.b)) + g.b
    out += struct.pack("<H", len(vm.timers))
    for active, periodic, period, remaining, owner in vm.timers:
        if not active:                               # a stopped timer is all zeros (owner -1)
            out += struct.pack("<Biqh", 0, 0, 0, -1)
        else:
            out += struct.pack("<Biqh", 1 | (2 if periodic else 0), period, remaining,
                               -1 if owner is None else owner)
    return bytes(out)


def restore(vm, data):
    """Load a snapshot into a loaded VM (fs_restore).  Raises ValueError."""
    r = Reader(data)
    if r.take(4) != SNAPSHOT_MAGIC:
        raise ValueError("not a FlowScript snapshot")
    if r.unpack("H") != SNAPSHOT_VERSION:
        raise ValueError("unsupported snapshot version")
    if r.unpack("I") != vm.image_hash:
        raise ValueError("the snapshot belongs to another image")
    state = r.unpack("h")
    n = r.unpack("H")
    if not -1 <= state < len(vm.prog.states) or n != len(vm.prog.globals):
        raise ValueError("the snapshot does not fit this image")
    gl = []
    for i in range(n):
        t = r.byte()
        if t != V.type_of(vm.prog.globals[i]):
            raise ValueError("global %d has another type in the snapshot" % i)
        if t == INT:
            gl.append(r.unpack("i"))
        elif t == FLOAT:
            gl.append(r.unpack("d"))
        elif t == TIME:
            gl.append(Time(r.unpack("q")))
        else:
            kind = r.byte()
            if kind == STR_EMPTY:
                gl.append(Str(b""))
            elif kind == STR_IMAGE:
                k = r.unpack("H")
                table = vm.string_table()
                if k >= len(table):
                    raise ValueError("bad string index in the snapshot")
                gl.append(Str(table[k].encode("utf-8", "surrogateescape")))
            elif kind == STR_RUNTIME:
                gl.append(r.take(r.unpack("H")))
            else:
                raise ValueError("bad string kind in the snapshot")
    nt = r.unpack("H")
    if nt != len(vm.prog.timers):
        raise ValueError("the snapshot does not fit this image")
    timers = []
    for _ in range(nt):
        flags = r.byte()
        period, remaining, owner = r.unpack("i"), r.unpack("q"), r.unpack("h")
        if not -1 <= owner < len(vm.prog.states):
            raise ValueError("bad timer owner in the snapshot")
        timers.append([bool(flags & 1), bool(flags & 2), period, remaining, None if owner < 0 else owner])
    if r.pos != len(data):
        raise ValueError("trailing bytes after the snapshot")
    vm._reset()
    for i, g in enumerate(gl):
        if isinstance(g, bytes):
            g = vm.alloc(g)                           # raises ValueFault 11/12 if it does not fit
        vm.globals[i] = g
    vm.state = None if state < 0 else state
    vm.timers = timers
    if vm.on_timer:
        for i, t in enumerate(timers):
            if t[0]:
                vm.on_timer(vm, i, 1, t[3] if t[3] > 0 else t[2], 1 if t[1] else 0)


# ---- recording ------------------------------------------------------------------------------
class Recorder:
    """Attached to a VM (``VM(record=sink)``): turns its inputs into frames for ``sink(bytes)``."""

    def __init__(self, vm, sink):
        self.vm, self.sink, self.steps = vm, sink, 0

    def emit(self, tag, payload=b"", flush=True):
        if flush:
            self.flush()
        self.sink(frame(tag, payload))

    def flush(self):
        if self.steps:
            n, self.steps = self.steps, 0
            self.sink(frame(STEPS, varint(n)))

    def session(self):
        vm = self.vm
        bits = bytearray((len(vm.bound) + 7) // 8)
        for i, fn in enumerate(vm.bound):
            if fn is not None:
                bits[i // 8] |= 1 << (i % 8)
        return struct.pack("<IB", vm.image_hash, 1 if vm.on_timer else 0) + bytes(bits)

    def start(self):
        self.emit(START, self.session())

    def keyframe(self):
        self.emit(KEYFRAME, self.session() + snapshot(self.vm))

    def post(self, event, values):
        ptypes = self.vm.prog.events[event][2]
        self.emit(POST, varint(event) + b"".join(enc_value(t, v) for t, v in zip(ptypes, values)))

    def postfail(self, code):
        self.emit(POSTFAIL, varint(code))

    def tick(self, ms):
        self.emit(TICK, varint(zigzag(ms)))

    def expired(self, timer):
        self.emit(EXPIRED, varint(timer))

    def result(self, ext, value):
        self.emit(RESULT, varint(ext) + enc_value(self.vm.prog.externs[ext][3], value), flush=False)

    def xfault(self, ext, code, value=None):
        """An extern's result the VM refused; a str that was too long / didn't fit is kept."""
        tail = b""
        if code in (V.OUT_OF_STRINGS, V.STRING_TOO_LONG) and self.vm.prog.externs[ext][3] == STR:
            b = value.b if isinstance(value, Str) else value.encode("utf-8") if isinstance(value, str) else bytes(value)
            tail = varint(len(b)) + b
        self.emit(XFAULT, varint(ext) + bytes([code]) + tail, flush=False)

    def clock(self, ms):
        self.emit(CLOCK, varint(zigzag(ms)), flush=False)

    def reset(self):
        self.emit(RESET)

    def setg(self, index, value):
        self.emit(SETG, varint(index) + enc_value(V.type_of(value), value))


# ---- replay ---------------------------------------------------------------------------------
class ReplayError(ValueError):
    """The recording does not fit the program, or went out of step with it."""


class Replay:
    """Re-run a recording against its program.

        rp = Replay(program, on_log=..., on_emit=..., on_fault=..., on_timer=..., on_extern=...)
        rp.feed(data)          # any number of times, with frames as they arrive
        rp.vm.state, rp.vm.globals, rp.vm.state_name()

    on_extern(name, args, result) sees every extern call (result None for an extern that
    returns nothing).  Until a START or KEYFRAME frame arrives, other frames are skipped
    (``skipped`` counts them): a viewer can join a running stream at its next keyframe.
    """

    def __init__(self, program, on_log=None, on_emit=None, on_fault=None, on_timer=None, on_extern=None,
                 on_step=None, **limits):
        from .bytecode import Program
        from .vm import VM
        if isinstance(program, (bytes, bytearray)):
            raw = bytes(program)
            program = Program.from_bytes(raw)
            hashes = {image_hash(raw)}
        elif getattr(program, "image", None):
            hashes = {image_hash(program.image)}
        else:
            hashes = {image_hash(program.to_bytes(debug=False))}
            if program.lines or program.global_names:
                hashes.add(image_hash(program.to_bytes(debug=True)))
        self.prog = program
        self.hashes = hashes
        self.results = []            # pending extern results / clock reads, in order
        self.buf = b""
        self.started = False
        self.skipped = 0
        self.on_extern = on_extern
        self.on_step = on_step
        self._timer_cb = on_timer
        self.vm = VM(program, {e[0]: self._stub(i) for i, e in enumerate(program.externs)},
                     on_emit=on_emit, on_fault=on_fault, on_log=on_log, clock=self._clock, **limits)

    # what the program asks the "host" during replay comes from the recording
    def _next(self, kind):
        if not self.results:
            raise ReplayError("the recording has no %s here: it is out of step with the program" % kind)
        k, v = self.results.pop(0)
        if k != kind:
            raise ReplayError("expected %s in the recording, found %s" % (kind, k))
        return v

    def _stub(self, i):
        name, _argc, _pt, ret = self.prog.externs[i]

        def stub(vm, args):
            if ret == NONE_TYPE:
                if self.on_extern:
                    self.on_extern(name, list(args), None)
                return 0
            ext, v = self._next("result")
            if ext != i:
                raise ReplayError("the recording returns for extern %d, the program calls %d" % (ext, i))
            if isinstance(v, Fault):
                if self.on_extern:
                    self.on_extern(name, list(args), v)
                raise v
            if self.on_extern:
                self.on_extern(name, list(args), Str(v) if isinstance(v, bytes) else v)
            return v
        return stub

    def _clock(self, vm):
        return self._next("clock")

    def feed(self, data):
        """Apply recorded frames (bytes; a frame cut off at the end waits for the next feed)."""
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
            self.apply(body[0], body[1:])
        self.buf = self.buf[r.pos:]

    def apply(self, tag, payload):
        from .vm import Fault as VMFault
        vm = self.vm
        r = Reader(payload)
        if tag in (START, KEYFRAME):
            h, flags = r.unpack("I"), r.byte()
            if h not in self.hashes:
                raise ReplayError("the recording is of another image (hash %08x)" % h)
            bits = r.take((len(vm.bound) + 7) // 8)
            for i in range(len(vm.bound)):
                vm.bound[i] = self._stub(i) if bits[i // 8] >> (i % 8) & 1 else None
            vm.on_timer = (self._timer_cb or (lambda *a: None)) if flags & 1 else None
            self.results = []
            if tag == START:
                vm._reset()
                vm.start()
            else:
                restore(vm, payload[r.pos:])
            self.started = True
            return
        if not self.started:
            self.skipped += 1
            return
        if tag == RESULT:
            ext = r.varint()
            if ext >= len(self.prog.externs):
                raise ReplayError("bad extern index in the recording")
            self.results.append(("result", (ext, dec_value(self.prog.externs[ext][3], r))))
        elif tag == XFAULT:
            ext = r.varint()
            f = VMFault(r.byte())
            f.value = Str(r.take(r.varint())) if r.pos < len(payload) else None
            self.results.append(("result", (ext, f)))
        elif tag == CLOCK:
            self.results.append(("clock", V.wrap64(r.svarint())))
        elif tag == STEPS:
            for _ in range(r.varint()):
                rv = vm.step()
                if self.on_step:
                    self.on_step(rv)
        elif tag == POST:
            ev = r.varint()
            if ev >= len(self.prog.events):
                raise ReplayError("bad event index in the recording")
            vals = [dec_value(t, r) for t in self.prog.events[ev][2]]
            vm.post(ev, *vals)
        elif tag == POSTFAIL:
            vm.report_fault(r.varint(), vm.cur_pc if vm.handler is not None else -1)
        elif tag == TICK:
            vm.tick(r.svarint())
        elif tag == EXPIRED:
            vm.timer_expired(r.varint())
        elif tag == RESET:
            vm.reset()
        elif tag == SETG:
            i = r.varint()
            if i >= len(vm.globals):
                raise ReplayError("bad global index in the recording")
            vm.set_global(i, dec_value(V.type_of(vm.globals[i]), r))
        else:
            raise ReplayError("unknown frame tag %d" % tag)


from .vm import Fault  # noqa: E402  (at the end: vm imports this module)
