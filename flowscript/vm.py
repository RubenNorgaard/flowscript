"""Reference FlowScript virtual machine (Python).

Behaviour is normative: the C VM (c/flowscript.c) must produce identical traces.
"""

import struct

from .bytecode import Program, BY_CODE, OPERAND_SIZE, EV_BOOT, EV_ENTER, EV_EXIT, FAULTS
from .intops import wrap, div, mod, shl, shr, DivZero

STACK_SIZE = 64
MAX_FRAMES = 16
QUEUE_SIZE = 16
BUDGET = 10000
MAX_CHAIN = 256          # dispatches in a row without the VM becoming idle

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
    externs:  dict name -> callable(vm, args:list[int]) -> int
    on_emit:  callable(vm, event_name, args) or None
    on_fault: callable(vm, code, pc) or None
    on_timer: callable(vm, timer, start, ms, periodic) or None. When set the host runs the
              timers: the VM asks it to start/stop them and the host calls
              timer_expired() when one fires; tick() then does nothing.
    """

    def __init__(self, prog, externs=None, on_emit=None, on_fault=None, on_timer=None,
                 stack_size=STACK_SIZE, max_frames=MAX_FRAMES, queue_size=QUEUE_SIZE, budget=BUDGET,
                 max_chain=MAX_CHAIN):
        if isinstance(prog, (bytes, bytearray)):
            prog = Program.from_bytes(prog)
        self.prog = prog
        self.code = prog.code
        self.externs = externs or {}
        self.on_emit = on_emit
        self.on_fault = on_fault
        self.on_timer = on_timer
        self.stack_size = stack_size
        self.max_frames = max_frames
        self.queue_size = queue_size
        self.budget = budget
        self.max_chain = max_chain
        self.bound = []
        for name, _argc in prog.externs:
            self.bound.append(self.externs.get(name))
        self.reset()

    def unbound_externs(self):
        return [self.prog.externs[i][0] for i, f in enumerate(self.bound) if f is None]

    def reset(self):
        for i, t in enumerate(getattr(self, "timers", [])):
            if t[0] and self.on_timer:
                self.on_timer(self, i, 0, 0, 0)
        self.globals = list(self.prog.globals)
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
        return self

    def post(self, event, *args):
        if isinstance(event, str):
            idx = self.prog.event_index(event)
            if idx < 0:
                raise KeyError("unknown event %r" % event)
            event = idx
        if len(self.queue) >= self.queue_size:
            self.report_fault(7, self.cur_pc if self.handler is not None else -1)
            return False
        argc = self.prog.events[event][1]
        a = [wrap(int(x)) for x in args[:argc]]
        a += [0] * (argc - len(a))
        self.queue.append((event, a))
        return True

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
        if not t[1]:
            t[0] = False
        return self.post(self.prog.timers[self.timer_index(timer)])

    def stop_timer(self, i):
        if self.timers[i][0]:
            self.timers[i][0] = False
            if self.on_timer:
                self.on_timer(self, i, 0, 0, 0)

    def tick(self, ms):
        if self.on_timer:
            return
        for i, t in enumerate(self.timers):
            if not t[0]:
                continue
            t[3] -= ms
            if t[3] <= 0:
                self.post(self.prog.timers[i])
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
        if self.boot_pending:
            self.boot_pending = False
            h = p.find_handler(None, EV_BOOT)
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
            if self.state is not None:          # leaving: stop the old state's timers
                for i, t in enumerate(self.timers):
                    if t[4] == self.state:
                        self.stop_timer(i)
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
                or bool(self.queue))

    def step(self):
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
        if kind is None:
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
        else:
            arg = struct.unpack_from("<i", code, pc + 1)[0]
            npc = pc + 5
        self.pc = npc
        push, pop = self.push, self.pop

        if name in ("PUSH", "PUSH8", "PUSHS"):
            push(arg)
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
            b = pop()
            a = pop()
            try:
                push(BINOPS[name](a, b))
            except DivZero:
                raise Fault(3)
        elif name == "NEG":
            push(wrap(-pop()))
        elif name == "BNOT":
            push(wrap(~pop()))
        elif name == "NOT":
            push(0 if pop() else 1)
        elif name == "BOOL":
            push(1 if pop() else 0)
        elif name == "JMP":
            self.pc = arg
        elif name == "JZ":
            if pop() == 0:
                self.pc = arg
        elif name == "JNZ":
            if pop() != 0:
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
            argc = self.prog.externs[arg][1]
            if len(self.stack) < argc:
                raise Fault(2)
            args = self.stack[len(self.stack) - argc:] if argc else []
            del self.stack[len(self.stack) - argc:]
            fn = self.bound[arg]
            if fn is None:
                raise Fault(8)
            r = fn(self, list(args))
            push(wrap(int(r or 0)))
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
            argc = self.prog.events[arg][1]
            if len(self.stack) < argc:
                raise Fault(2)
            args = self.stack[len(self.stack) - argc:] if argc else []
            del self.stack[len(self.stack) - argc:]
            if self.on_emit:
                self.on_emit(self, self.prog.events[arg][0], list(args))
            self.post(arg, *args)
        elif name in ("TSTART", "TEVERY"):
            if arg >= len(self.timers):
                raise Fault(5)
            ms = pop()
            t = self.timers[arg]
            t[0] = True
            t[1] = name == "TEVERY"
            t[2] = ms
            t[3] = ms
            t[4] = self.handler[0]
            if self.on_timer:
                self.on_timer(self, arg, 1, ms, 1 if t[1] else 0)
        elif name == "TSTOP":
            if arg >= len(self.timers):
                raise Fault(5)
            self.stop_timer(arg)
        elif name == "NOP":
            pass
        else:
            raise Fault(5)
        return RUNNING

    # -- introspection -----------------------------------------------------------------
    def state_name(self):
        return None if self.state is None else self.prog.states[self.state]


BINOPS = {
    "ADD": lambda a, b: wrap(a + b), "SUB": lambda a, b: wrap(a - b),
    "MUL": lambda a, b: wrap(a * b), "DIV": div, "MOD": mod,
    "BAND": lambda a, b: wrap(a & b), "BOR": lambda a, b: wrap(a | b),
    "BXOR": lambda a, b: wrap(a ^ b), "SHL": shl, "SHR": shr,
    "EQ": lambda a, b: int(a == b), "NE": lambda a, b: int(a != b),
    "LT": lambda a, b: int(a < b), "LE": lambda a, b: int(a <= b),
    "GT": lambda a, b: int(a > b), "GE": lambda a, b: int(a >= b),
}
