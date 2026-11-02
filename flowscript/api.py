"""The host-facing API: compile, load, run.

    import flowscript

    program = flowscript.compile(source)          # raises flowscript.CompileError
    program.warnings                              # [Diagnostic(line, col, message)]
    data = program.to_bytes()                     # the .fsb image (also: program.save(path))

    program = flowscript.load(data)               # bytes, a path or a binary file

    machine = flowscript.Machine(program, externs={"param": params.get, ...}, clock=time.time)
    machine.start()                               # boot + enter the initial state
    machine.send("tick")                          # post an event and run until idle
    machine.state, machine["aggression"]          # current state, a global

Values crossing the boundary are plain Python: int, float, str and timezone-aware
``datetime`` (FlowScript ``time``).  Hosts never see the VM's internal representations.
"""

import datetime as _dt
import os

from .bytecode import Program, FAULTS
from .compiler import compile_ast
from .lexer import FlowError
from .parser import parse
from . import values as V
from .vm import VM

__all__ = ["compile", "load", "Machine", "CompileError", "Diagnostic", "Fault"]

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


class CompileError(FlowError):
    """The source has an error.  ``line`` and ``col`` (1-based, 0 = unknown) locate it."""


class Diagnostic:
    """A compiler warning: the program compiles, but probably not as intended."""
    __slots__ = ("line", "col", "message")

    def __init__(self, line, col, message):
        self.line, self.col, self.message = line, col, message

    def __repr__(self):
        return "Diagnostic(line=%d, %r)" % (self.line, self.message)

    def __str__(self):
        return "line %d: %s" % (self.line, self.message)


class Fault:
    """A run-time fault: the handler it happened in was aborted, the machine carries on."""
    __slots__ = ("code", "name", "pc", "line", "handler")

    def __init__(self, code, pc, line, handler):
        self.code, self.pc, self.line, self.handler = code, pc, line, handler
        self.name = FAULTS.get(code, "fault %d" % code)

    def __repr__(self):
        where = ("line %d" % self.line) if self.line else ("pc %d" % self.pc)
        return "Fault(%d %s, %s%s)" % (self.code, self.name, where,
                                       ", in %s" % self.handler if self.handler else "")

    __str__ = __repr__


# ------------------------------------------------------------------------------ compile / load
def compile(source, *, filename=None):
    """Compile FlowScript source to a ``Program``.

    The program keeps what hosts need besides the bytecode: ``source``, ``ast`` (for
    ``flowscript.to_svg``), ``warnings`` (a list of ``Diagnostic``) and debug information
    (source lines of faults, global names).  Raises ``CompileError``.
    """
    try:
        ast = parse(source)
        prog, dbg = compile_ast(ast)
    except FlowError as e:
        err = CompileError(e.msg, e.line, e.col)
        err.filename = filename
        raise err from None
    prog.source = source
    prog.ast = ast
    prog.debug_info = dbg
    prog.warnings = [Diagnostic(w["line"], w.get("col", 0), w["msg"]) for w in dbg.warnings]
    return prog


def load(data):
    """A ``Program`` from bytecode: bytes/bytearray/memoryview, a path, or a binary file object.

    Raises ``ValueError`` if the data is not a valid FlowScript image.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    elif isinstance(data, (str, os.PathLike)):
        with open(data, "rb") as f:
            raw = f.read()
    elif hasattr(data, "read"):
        raw = data.read()
    else:
        raise TypeError("load() takes bytes, a path or a binary file, not %s" % type(data).__name__)
    return Program.from_bytes(raw)


# ------------------------------------------------------------------------------ values
def _to_py(v):
    if isinstance(v, V.Str):
        return v.text
    if isinstance(v, V.Time):
        return _EPOCH + _dt.timedelta(milliseconds=v.ms)
    return v


def _to_vm(v):
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            raise ValueError("FlowScript times are UTC: pass a timezone-aware datetime")
        delta = v - _EPOCH
        return V.Time((delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000)
    if isinstance(v, bool):
        return int(v)
    return v


# ------------------------------------------------------------------------------ machine
class Machine:
    """A running FlowScript program.

    program:  a ``Program`` (``compile``/``load``) or bytecode bytes.
    externs:  {name: callable(*args)} for the program's ``extern`` declarations.  Arguments and
              return values are int, float, str or timezone-aware datetime; an int is accepted
              where a float is declared.  Every extern must be provided (``ValueError`` otherwise).
    clock:    callable() -> seconds since 1970 (as ``time.time``) or a datetime; feeds ``now()``.
              Default: ``time.time``.
    on_event: callable(name, args) for every ``raise_event`` (args as Python values), before
              the event is queued for the script itself.
    on_fault: callable(Fault).  Faults are also kept in ``faults`` (the last 100).
    on_log:   callable(level, message, template, values) for every log statement
              (``warning "x is {x}"``): level "debug"/"info"/"warning"/"error", the rendered
              message, the template ('{}' per value) and the values as Python.  Without it,
              log statements do nothing (e.g. ``on_log=lambda lv, msg, *_: logger.log(...)``).
    on_timer: None: timers run inside the machine, driven by ``advance(ms)``.  Or
              callable(name, start, ms, periodic): the host runs the timers (start=False stops
              one) and calls ``timer_expired(name)`` when one fires.
    limits:   VM limits (stack_size, queue_size, budget, str_slots, str_len, ...), defaulting
              to the C VM's defaults so a script that runs here also runs there.
    """

    def __init__(self, program, *, externs=None, clock=None, on_event=None, on_fault=None,
                 on_timer=None, on_log=None, **limits):
        if isinstance(program, (bytes, bytearray, memoryview)):
            program = load(program)
        self.program = program
        externs = dict(externs or {})
        missing = [name for name, *_ in program.externs if name not in externs]
        if missing:
            raise ValueError("no host function for extern(s): %s" % ", ".join(missing))
        unknown = sorted(set(externs) - {e[0] for e in program.externs})
        if unknown:
            raise ValueError("the program declares no extern(s) %s" % ", ".join(unknown))
        self.faults = []
        self._on_fault = on_fault
        self._on_event = on_event
        self._on_timer = on_timer
        self._on_log = on_log
        if clock is None:
            import time
            clock = time.time
        self._clock = clock
        names = list(program.global_names) if getattr(program, "global_names", None) else []
        self._globals = {n: i for i, n in enumerate(names) if n}
        self.vm = VM(program, {name: self._wrap(fn) for name, fn in externs.items()},
                     on_emit=self._emit, on_fault=self._fault,
                     on_timer=self._timer if on_timer else None, clock=self._now,
                     on_log=self._log if on_log else None, **limits)

    # -- glue ---------------------------------------------------------------------------
    @staticmethod
    def _wrap(fn):
        return lambda vm, args: _to_vm(fn(*[_to_py(a) for a in args]))

    def _now(self, vm):
        t = self._clock()
        if isinstance(t, _dt.datetime):
            return _to_vm(t).ms
        return int(round(t * 1000))

    def _emit(self, vm, name, args):
        if self._on_event:
            self._on_event(name, [_to_py(a) for a in args])

    def _log(self, vm, level, template, values):
        message = V.format_log(template, values).decode("utf-8", "surrogateescape")
        self._on_log(V.LOG_LEVELS[level], message, template, [_to_py(v) for v in values])

    def _timer(self, vm, timer, start, ms, periodic):
        self._on_timer(self.program.events[self.program.timers[timer]][0], bool(start), ms, bool(periodic))

    def _fault(self, vm, code, pc):
        line = 0
        lines = getattr(self.program, "lines", None) or []
        for p, ln, _node in lines:
            if p > pc:
                break
            line = ln
        handler = None
        dbg = getattr(self.program, "debug_info", None)
        if dbg is not None and pc >= 0:
            r = dbg.routine_at(pc)
            handler = r["name"] if r else None
        f = Fault(code, pc, line if pc >= 0 else 0, handler)
        self.faults.append(f)
        del self.faults[:-100]
        if self._on_fault:
            self._on_fault(f)

    # -- running ------------------------------------------------------------------------
    def start(self):
        """Run ``on boot`` and enter the initial state.  Returns the machine."""
        self.vm.start()
        self.vm.run()
        return self

    def post(self, event, *args):
        """Queue an event (not run yet).  False if the queue or string memory was full."""
        return self.vm.post(event, *[_to_vm(a) for a in args])

    def run(self, max_steps=1000000):
        """Run queued work until idle; returns the number of steps."""
        return self.vm.run(max_steps)

    def send(self, event, *args):
        """Post an event and run until idle.  False if it could not be queued."""
        ok = self.post(event, *args)
        self.run()
        return ok

    def advance(self, ms):
        """Built-in timers: let ``ms`` milliseconds pass.

        Time moves from one timer expiry to the next and the handlers run in between, so
        every expiry fires in order (``every 1s`` fires three times in ``advance(3000)``)
        and timers that handlers start or stop take effect at the right moment.
        """
        left = int(ms)
        while left > 0:
            due = [t[3] for t in self.vm.timers if t[0]]
            step = min([left] + [max(1, d) for d in due])
            self.vm.tick(step)
            self.vm.run()
            left -= step

    def timer_expired(self, name):
        """Host-run timers: the timer ``name`` fired.  Runs its handler; False if it was stopped."""
        r = self.vm.timer_expired(self.vm.timer_index(name))
        self.vm.run()
        return r

    def reset(self):
        """Back to the initial values (globals, no state, empty queue).  Call ``start()`` again."""
        self.vm.reset()

    # -- state ----------------------------------------------------------------------------
    @property
    def state(self):
        """Name of the current state, or None (before ``start()``, or a program without states)."""
        return self.vm.state_name()

    @property
    def idle(self):
        return not self.vm.has_work() and not self.vm.active

    @property
    def globals(self):
        """{name: value} of the program's variables (state variables as ``state.name``)."""
        return {n: _to_py(self.vm.globals[i]) for n, i in self._globals.items()}

    def __getitem__(self, name):
        return _to_py(self.vm.globals[self._global_index(name)])

    def __setitem__(self, name, value):
        self.vm.set_global(self._global_index(name), _to_vm(value))

    def _global_index(self, name):
        if name not in self._globals:
            if not self._globals:
                raise KeyError("%r: this program has no global names (compile it, or load "
                               "bytecode compiled with debug information)" % name)
            raise KeyError("no global variable %r" % name)
        return self._globals[name]

    def __repr__(self):
        return "<Machine %s state=%s>" % (self.program.name or "program", self.state)
