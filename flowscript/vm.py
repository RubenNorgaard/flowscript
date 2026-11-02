"""Reference FlowScript virtual machine (Python).

Behaviour is normative: the C VM (c/flowscript.c) must produce identical traces.
"""

import struct

from .bytecode import Program, BY_CODE, OPERAND_SIZE, EV_BOOT, EV_ENTER, EV_EXIT, FAULTS
from .intops import wrap, DivZero
from . import values as V
from .values import INT, FLOAT, STR, TIME, NONE_TYPE, Str, Time

STACK_SIZE = 64
MAX_FRAMES = 16
QUEUE_SIZE = 16
BUDGET = 10000
LEAVING = -2             # timer owner while its state is being left (see dispatch)
MAX_CHAIN = 256          # dispatches in a row without the VM becoming idle
STR_SLOTS = V.STR_SLOTS  # run-time strings alive at once
STR_LEN = V.STR_LEN      # bytes per run-time string

# step() results
IDLE = "idle"            # nothing to do
DISPATCHED = "dispatch"  # a handler was started (no instruction executed)
RUNNING = "running"      # an instruction was executed, handler still active
DONE = "done"            # an instruction was executed and the handler finished
FAULT = "fault"          # the instruction faulted, handler aborted


class Fault(Exception):
    def __init__(self, code):
        super().__init__(FAULTS.get(code, "fault"))
        self.code = code


class VM:
    """
    externs:  dict name -> callable(vm, args) -> value. Values are int, float, values.Str
              (.b bytes, .text) and values.Time (.ms); a str extern may also return a
              Python str/bytes. An extern declared without a return type may return anything.
    clock:    callable(vm) -> int ms since 1970 (UTC) for now(); default: always 0
    on_emit:  callable(vm, event_name, args) or None
    on_fault: callable(vm, code, pc) or None
    on_log:   callable(vm, level, template, values) or None: a log statement (level 0 debug,
              1 info, 2 warning, 3 error; template a str with '{}' per value, '{{' '}}' braces;
              values.format_log renders it).  Without it, log statements do nothing.
    on_timer: callable(vm, timer, start, ms, periodic) or None. When set the host runs the
              timers: the VM asks it to start/stop them and the host calls
              timer_expired() when one fires; tick() then does nothing.
    """

    def __init__(self, prog, externs=None, on_emit=None, on_fault=None, on_timer=None, on_log=None, record=None,
                 stack_size=STACK_SIZE, max_frames=MAX_FRAMES, queue_size=QUEUE_SIZE, budget=BUDGET,
                 max_chain=MAX_CHAIN, str_slots=STR_SLOTS, str_len=STR_LEN, clock=None):
        from . import record as R
        if isinstance(prog, (bytes, bytearray)):
            self.image_hash = R.image_hash(prog)
            prog = Program.from_bytes(prog)
            self.image_strings = prog.strings
        elif getattr(prog, "image", None):     # loaded from bytes: those exact bytes
            self.image_hash = R.image_hash(prog.image)
            self.image_strings = prog.strings
        else:
            image = prog.to_bytes(debug=False)
            self.image_hash = R.image_hash(image)
            self.image_strings = None          # the image's string table, built when first needed
            self._image = image
        self.prog = prog
        self.code = prog.code
        self.externs = externs or {}
        self.on_emit = on_emit
        self.on_fault = on_fault
        self.on_timer = on_timer
        self.on_log = on_log
        self.stack_size = stack_size
        self.max_frames = max_frames
        self.queue_size = queue_size
        self.budget = budget
        self.max_chain = max_chain
        self.str_slots = str_slots
        self.str_len = str_len
        self.clock = clock
        self.bound = []
        for ext in prog.externs:
            self.bound.append(self.externs.get(ext[0]))
        self.rec = None
        self._reset()
        self.rec = R.Recorder(self, record) if record else None

    def unbound_externs(self):
        return [self.prog.externs[i][0] for i, f in enumerate(self.bound) if f is None]

    def reset(self):
        """Back to the initial values (globals, no state, empty queue).  Call start() again."""
        self._reset()
        if self.rec:
            self.rec.reset()

    def _reset(self):
        for i, t in enumerate(getattr(self, "timers", [])):
            if t[0] and self.on_timer:
                self.on_timer(self, i, 0, 0, 0)
        self.globals = list(self.prog.globals)
        self.pool = [None] * self.str_slots   # run-time strings: slot -> bytes, None = free
        self.stack = []
        self.frames = []          # [return_pc, base]
        self.pc = 0
        self.queue = []           # (event, args)
        self.state = None         # current state index or None
        self.pending = None       # requested goto_state target
        self.transition = None    # target being entered (after exit handler)
        self.in_exit = False
        self.handler = None       # active handler tuple
        self.steps = 0            # instructions executed in this handler
        # active, periodic, period, remaining, owner (state whose handler started it, or None)
        self.timers = [[False, False, 0, 0, None] for _ in self.prog.timers]
        self.boot_pending = False
        self.cur_pc = -1
        self.faults = 0
        self.chain = 0            # dispatches since the VM was last idle

    # -- host API ------------------------------------------------------------------
    def start(self):
        """Queue boot and the entry into the initial state."""
        self.boot_pending = True
        if self.prog.states:
            self.pending = 0
        if self.rec:
            self.rec.start()
        return self

    # -- snapshots and recording (record.py) ------------------------------------------------
    def snapshot(self):
        """The state while idle (state, globals, timers) as bytes; RuntimeError when busy."""
        from . import record as R
        return R.snapshot(self)

    def restore(self, data):
        """Resume from a snapshot of this image (instead of start()).  ValueError if it doesn't fit."""
        from . import record as R
        try:
            R.restore(self, data)
        except V.ValueFault as f:
            raise ValueError("the snapshot's strings do not fit (fault %d)" % f.code)
        if self.rec:
            self.rec.keyframe()             # a recording says where the run continues from

    def record_keyframe(self):
        """Recording: a keyframe (session + snapshot), where a viewer can start replaying."""
        if self.rec:
            self.rec.keyframe()

    def record_flush(self):
        """Recording: hand over the steps counted so far (e.g. before sending a packet)."""
        if self.rec:
            self.rec.flush()

    def string_table(self):
        """The string table of the image as the C VM sees it (names and initial values included)."""
        if self.image_strings is None:
            self.image_strings = Program.from_bytes(self._image).strings
        return self.image_strings

    def post(self, event, *args):
        """Queue an event. Arguments are converted to the event's parameter types (an int
        is accepted for a float, a Python str/bytes for a str). Returns False if the queue
        or the string memory is full (a fault is reported). Raises TypeError for a wrong type."""
        if isinstance(event, str):
            idx = self.prog.event_index(event)
            if idx < 0:
                raise KeyError("unknown event %r" % event)
            event = idx
        return self._post(event, args, record=bool(self.rec))

    def _post(self, event, args, record=False):
        pc = self.cur_pc if self.handler is not None else -1
        if len(self.queue) >= self.queue_size:
            if record:
                self.rec.postfail(7)
            self.report_fault(7, pc)
            return False
        _name, argc, ptypes = self.prog.events[event]
        entry = (event, [0] * argc)
        self.queue.append(entry)              # a root while its strings are allocated
        try:
            for i in range(argc):
                v = args[i] if i < len(args) else V.zero(ptypes[i])
                entry[1][i] = self.host_value(v, ptypes[i])
        except V.ValueFault as f:
            self.queue.remove(entry)
            if f.code == V.TYPE_MISMATCH:
                raise TypeError("event %s: argument has the wrong type" % _name)
            if record:
                self.rec.postfail(f.code)
            self.report_fault(f.code, pc)
            return False
        if record:
            self.rec.post(event, entry[1])
        return True

    def host_value(self, v, t):
        """A value from the host as type t (allocates a str). Raises ValueFault."""
        if t == STR and isinstance(v, (str, bytes)):
            return self.alloc(v.encode("utf-8") if isinstance(v, str) else v)
        try:
            vt = V.type_of(v)
        except TypeError:
            raise V.ValueFault(V.TYPE_MISMATCH)
        if t == FLOAT and vt == INT:
            return float(v)
        if vt != t:
            raise V.ValueFault(V.TYPE_MISMATCH)
        if vt == INT:
            return wrap(int(v))
        if vt == STR and v.slot is not None and self.pool[v.slot] is None:
            raise V.ValueFault(V.TYPE_MISMATCH)       # a stale run-time string
        return v

    def set_global(self, index, value):
        """Debugger/host: set a global (converted to its current type)."""
        self.globals[index] = self.host_value(value, V.type_of(self.globals[index]))
        if self.rec:
            self.rec.setg(index, self.globals[index])

    # -- strings -------------------------------------------------------------------------
    def alloc(self, b):
        """A new run-time string. Raises ValueFault 12 (too long) or 11 (no free slot)."""
        if len(b) > self.str_len:
            raise V.ValueFault(V.STRING_TOO_LONG)
        for attempt in (0, 1):
            for i, used in enumerate(self.pool):
                if used is None:
                    self.pool[i] = b
                    return Str(b, i)
            if attempt == 0:
                self.collect()
        raise V.ValueFault(V.OUT_OF_STRINGS)

    def collect(self):
        """Mark and sweep: free every slot not referenced by a global, the stack or the queue."""
        live = set()
        for v in self.globals + self.stack:
            if isinstance(v, Str) and v.slot is not None:
                live.add(v.slot)
        for _ev, args in self.queue:
            for v in args:
                if isinstance(v, Str) and v.slot is not None:
                    live.add(v.slot)
        for i in range(len(self.pool)):
            if i not in live:
                self.pool[i] = None

    def strings_in_use(self):
        return sum(1 for b in self.pool if b is not None)

    def timer_index(self, timer):
        if isinstance(timer, str):
            for i, ev in enumerate(self.prog.timers):
                if self.prog.events[ev][0] == timer:
                    return i
            raise KeyError("unknown timer %r" % timer)
        return timer

    def timer_expired(self, timer):
        """Host-managed timers: the host's timer fired. Returns True if the event was queued."""
        t = self.timers[self.timer_index(timer)]
        if not t[0]:
            return False                        # stopped by the script meanwhile: ignore
        if self.rec:
            self.rec.expired(self.timer_index(timer))
        if not t[1]:
            t[0] = False
        return self._post(self.prog.timers[self.timer_index(timer)], ())

    def stop_timer(self, i):
        if self.timers[i][0]:
            self.timers[i][0] = False
            if self.on_timer:
                self.on_timer(self, i, 0, 0, 0)

    def tick(self, ms):
        if self.rec:
            self.rec.tick(ms)
        if self.on_timer:
            return
        for i, t in enumerate(self.timers):
            if not t[0]:
                continue
            t[3] -= ms
            if t[3] <= 0:
                self._post(self.prog.timers[i], ())
                if t[1]:
                    t[3] += t[2]
                    if t[3] <= 0:
                        t[3] = t[2]
                else:
                    t[0] = False

    def run(self, max_steps=1000000):
        """Run until idle. Returns number of steps."""
        n = 0
        while n < max_steps:
            r = self.step()
            if r == IDLE:
                break
            n += 1
        return n

    @property
    def active(self):
        return self.handler is not None

    # -- dispatch --------------------------------------------------------------------
    def begin(self, h, args):
        _st, _ev, argc, nlocals, off = h
        self.handler = h
        self.stack = list(args[:argc]) + [0] * (argc - len(args))
        self.stack += [0] * (nlocals - len(self.stack))
        self.frames = [[-1, 0]]
        self.pc = off
        self.steps = 0

    def dispatch(self):
        """Start the next handler. Returns True if one was started or a transition progressed."""
        p = self.prog
        # A transition marked the old state's timers LEAVING; the new state's 'on enter' has
        # run (or there was none), so the ones it did not start again stop now.
        for i, t in enumerate(self.timers):
            if t[0] and t[4] == LEAVING:
                self.stop_timer(i)
        if self.boot_pending:
            self.boot_pending = False
            h = p.find_handler(0 if p.states else None, EV_BOOT)   # 'on boot' lives in the initial state
            if h is not None:
                self.begin(h, [])
                return True
        if self.pending is not None:
            self.transition = self.pending
            self.pending = None
            if self.state is not None:
                h = p.find_handler(self.state, EV_EXIT)
                if h is not None and h[0] == self.state:
                    self.in_exit = True
                    self.begin(h, [])
                    return True
        if self.transition is not None:
            # Leaving: the old state's timers stop once the new state has been entered, unless
            # its 'on enter' starts them again (a periodic timer restarted with the same period
            # keeps running, phase and all).  Before the first entry, the timers belong to the
            # initial state ('on boot'): they go too if the program starts in another state.
            old = self.state if self.state is not None else 0
            if not (self.state is None and self.transition == old):
                for t in self.timers:
                    if t[0] and t[4] == old:
                        t[4] = LEAVING
            self.state = self.transition
            self.transition = None
            h = p.find_handler(self.state, EV_ENTER)
            if h is not None and h[0] == self.state:
                self.begin(h, [])
            return True
        while self.queue:
            ev, args = self.queue.pop(0)
            h = p.find_handler(self.state, ev)
            if h is not None:
                self.begin(h, args)
                return True
        return False

    def finish(self):
        self.handler = None
        self.frames = []
        self.stack = []
        self.in_exit = False

    def report_fault(self, code, pc):
        self.faults += 1
        if self.on_fault:
            self.on_fault(self, code, pc)

    # -- execution -------------------------------------------------------------------
    def has_work(self):
        return (self.boot_pending or self.pending is not None or self.transition is not None
                or bool(self.queue) or any(t[0] and t[4] == LEAVING for t in self.timers))

    def step(self):
        if self.rec:
            self.rec.steps += 1
        if self.handler is None:
            if self.chain >= self.max_chain and self.has_work():
                # the script keeps itself busy (goto_state/emit loop): drop the pending work
                self.pending = self.transition = None
                self.boot_pending = False
                self.queue = []
                self.chain = 0
                self.report_fault(9, -1)
                return IDLE
            if self.dispatch():
                self.chain += 1
                return DISPATCHED
            self.chain = 0
            return IDLE
        try:
            return self.execute()
        except Fault as f:
            self.report_fault(f.code, self.cur_pc)
            self.finish()
            return FAULT

    def pop(self):
        if not self.stack or len(self.stack) <= 0:
            raise Fault(2)
        return self.stack.pop()

    def push(self, v):
        if len(self.stack) >= self.stack_size:
            raise Fault(1)
        self.stack.append(v)

    def execute(self):
        code = self.code
        pc = self.pc
        self.cur_pc = pc
        self.steps += 1
        if self.steps > self.budget:
            raise Fault(4)
        if pc >= len(code):
            raise Fault(5)
        op = code[pc]
        ent = BY_CODE.get(op)
        if ent is None:
            raise Fault(5)
        name, kind = ent
        if pc + OPERAND_SIZE[kind] >= len(code):  # operand cut off by end of code
            raise Fault(5)
        if kind in ("f64", "i64"):
            arg = struct.unpack_from("<d" if kind == "f64" else "<q", code, pc + 1)[0]
            npc = pc + 9
        elif kind is None:
            arg = None
            npc = pc + 1
        elif kind == "u16":
            arg = code[pc + 1] | (code[pc + 2] << 8)
            npc = pc + 3
        elif kind == "u8":
            arg = code[pc + 1]
            npc = pc + 2
        elif kind == "i8":
            arg = struct.unpack_from("<b", code, pc + 1)[0]
            npc = pc + 2
        elif kind == "i32":
            arg = struct.unpack_from("<i", code, pc + 1)[0]
            npc = pc + 5
        self.pc = npc
        push, pop = self.push, self.pop

        if name in ("PUSH", "PUSH8"):
            push(arg)
        elif name == "PUSHF":
            push(arg)
        elif name == "PUSHT":
            push(Time(arg))
        elif name == "PUSHS":
            if arg >= len(self.prog.strings):
                raise Fault(5)
            push(Str(self.prog.strings[arg].encode("utf-8", "surrogateescape")))
        elif name == "POP":
            pop()
        elif name == "DUP":
            v = pop()
            push(v)
            push(v)
        elif name == "LOADG":
            if arg >= len(self.globals):
                raise Fault(5)
            push(self.globals[arg])
        elif name == "STOREG":
            if arg >= len(self.globals):
                raise Fault(5)
            self.globals[arg] = pop()
        elif name == "LOADL":
            i = self.frames[-1][1] + arg
            if i >= len(self.stack):
                raise Fault(5)
            push(self.stack[i])
        elif name == "STOREL":
            v = pop()
            i = self.frames[-1][1] + arg
            if i >= len(self.stack):
                raise Fault(5)
            self.stack[i] = v
        elif name in BINOPS:
            if len(self.stack) < 2:
                raise Fault(2)
            a, b = self.stack[-2], self.stack[-1]
            op = BINOPS[name]
            ta, tb = V.type_of(a), V.type_of(b)
            if op in ("&", "|", "^", "<<", ">>") and not (ta == tb == INT):
                raise Fault(10)
            if (name == "CONCAT") != (ta == STR and tb == STR and op == "+"):
                raise Fault(10)             # strings are joined by CONCAT, numbers by ADD
            try:
                r = V.binary(op, a, b)
            except DivZero:
                raise Fault(3)
            except V.ValueFault as f:
                raise Fault(f.code)
            if isinstance(r, bytes):
                r = self.alloc_or_fault(r)      # the operands are still on the stack (roots)
            del self.stack[-2:]
            push(r)
        elif name == "NEG":
            v = pop()
            t = V.type_of(v)
            if t == INT:
                push(wrap(-v))
            elif t == FLOAT:
                push(-v)
            else:
                raise Fault(10)
        elif name == "BNOT":
            push(wrap(~self.pop_int()))
        elif name == "NOT":
            push(0 if self.pop_int() else 1)
        elif name == "BOOL":
            push(1 if self.pop_int() else 0)
        elif name == "JMP":
            self.pc = arg
        elif name == "JZ":
            if self.pop_int() == 0:
                self.pc = arg
        elif name == "JNZ":
            if self.pop_int() != 0:
                self.pc = arg
        elif name == "CALL":
            if arg >= len(self.prog.funcs):
                raise Fault(5)
            _n, argc, nlocals, off = self.prog.funcs[arg]
            if len(self.frames) >= self.max_frames:
                raise Fault(6)
            base = len(self.stack) - argc
            if base < 0:
                raise Fault(2)
            for _ in range(nlocals - argc):
                push(0)
            self.frames.append([npc, base])
            self.pc = off
        elif name == "RETN":
            if arg > 16:                            # MAX_RESULTS in the C VM
                raise Fault(5)
            if len(self.stack) < arg:
                raise Fault(2)
            vals = self.stack[len(self.stack) - arg:] if arg else []
            del self.stack[len(self.stack) - arg:]
            ret_pc, base = self.frames.pop()
            del self.stack[base:]
            if not self.frames:
                self.finish()
                return DONE
            self.pc = ret_pc
            for v in vals:
                push(v)
        elif name in ("RET", "RET0"):
            v = pop() if name == "RET" else 0
            ret_pc, base = self.frames.pop()
            del self.stack[base:]
            if not self.frames:
                self.finish()
                return DONE
            self.pc = ret_pc
            push(v)
        elif name == "XCALL":
            if arg >= len(self.bound):
                raise Fault(5)
            _xn, argc, ptypes, ret = self.prog.externs[arg]
            if len(self.stack) < argc:
                raise Fault(2)
            args = self.stack[len(self.stack) - argc:] if argc else []
            self.check_types(args, ptypes)
            fn = self.bound[arg]
            if fn is None:
                raise Fault(8)
            r = fn(self, list(args))      # the arguments stay on the stack (roots) meanwhile
            if ret == NONE_TYPE:
                r = 0
            else:
                try:
                    r = self.host_value(r, ret)
                except V.ValueFault as f:
                    if self.rec:
                        self.rec.xfault(arg, f.code, r)
                    raise Fault(f.code)
                if self.rec:
                    self.rec.result(arg, r)
            del self.stack[len(self.stack) - argc:]
            push(r)
        elif name == "BUILTIN":
            if arg >= len(V.BUILTINS):
                raise Fault(5)
            _bn, ptypes, _ret = V.BUILTINS[arg]
            argc = len(ptypes)
            if len(self.stack) < argc:
                raise Fault(2)
            args = self.stack[len(self.stack) - argc:] if argc else []
            self.check_types(args, ptypes)
            try:
                r = V.call_builtin(arg, args, self._now)
            except V.ValueFault as f:
                raise Fault(f.code)
            if isinstance(r, bytes):
                r = self.alloc_or_fault(r)
            del self.stack[len(self.stack) - argc:]
            push(r)
        elif name == "GOTO":
            if arg >= len(self.prog.states):
                raise Fault(5)
            if not self.in_exit:
                self.pending = arg
            self.finish()
            return DONE
        elif name == "EMIT":
            if arg >= len(self.prog.events):
                raise Fault(5)
            _en, argc, ptypes = self.prog.events[arg]
            if len(self.stack) < argc:
                raise Fault(2)
            args = list(self.stack[len(self.stack) - argc:]) if argc else []
            self.check_types(args, ptypes)
            if self.on_emit:
                self.on_emit(self, self.prog.events[arg][0], list(args))
            self._post(arg, args)
            del self.stack[len(self.stack) - argc:]
        elif name in ("TSTART", "TEVERY"):
            if arg >= len(self.timers):
                raise Fault(5)
            ms = self.pop_int()
            t = self.timers[arg]
            if name == "TEVERY" and t[0] and t[1] and t[2] == ms:
                t[4] = self.handler[0]      # already running with this period: keep it (and its phase)
                return RUNNING
            t[0] = True
            t[1] = name == "TEVERY"
            t[2] = ms
            t[3] = ms
            t[4] = self.handler[0]
            if self.on_timer:
                self.on_timer(self, arg, 1, ms, 1 if t[1] else 0)
        elif name in ("LOGD", "LOGI", "LOGW", "LOGE"):
            if arg >= len(self.prog.strings):
                raise Fault(5)
            template = self.prog.strings[arg]
            n = V.template_args(template)
            if n > 16:                              # MAX_LOG_ARGS in the C VM
                raise Fault(5)
            if len(self.stack) < n:
                raise Fault(2)
            if self.on_log:
                self.on_log(self, ("LOGD", "LOGI", "LOGW", "LOGE").index(name), template,
                            self.stack[len(self.stack) - n:] if n else [])
            del self.stack[len(self.stack) - n:]
        elif name == "TSTOP":
            if arg >= len(self.timers):
                raise Fault(5)
            self.stop_timer(arg)
        elif name == "NOP":
            pass
        else:
            raise Fault(5)
        return RUNNING

    def pop_int(self):
        v = self.pop()
        if V.type_of(v) != INT:
            raise Fault(10)
        return v

    def _now(self):
        ms = V.wrap64(self.clock(self)) if self.clock else 0
        if self.rec:
            self.rec.clock(ms)
        return ms

    def check_types(self, args, ptypes):
        for v, t in zip(args, ptypes):
            if V.type_of(v) != t:
                raise Fault(10)

    def alloc_or_fault(self, b):
        try:
            return self.alloc(b)
        except V.ValueFault as f:
            raise Fault(f.code)

    # -- introspection -----------------------------------------------------------------
    def state_name(self):
        return None if self.state is None else self.prog.states[self.state]


BINOPS = {
    "ADD": "+", "CONCAT": "+", "SUB": "-", "MUL": "*", "DIV": "/", "MOD": "%",
    "BAND": "&", "BOR": "|", "BXOR": "^", "SHL": "<<", "SHR": ">>",
    "EQ": "==", "NE": "!=", "LT": "<", "LE": "<=", "GT": ">", "GE": ">=",
}
