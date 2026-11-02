r"""Scenario runner with a canonical trace format.

The C runner (c/fsrun.c) implements the exact same format; the conformance test
compares the two traces line by line.

Scenario file (one command per line, '#' comments outside quotes):
    extern NAME VALUE     set the return value of a stubbed extern (default: zero of its type)
    post EVENT [ARGS...]  queue an event, then run until idle
    tick MS               advance timers and the clock, then run until idle
    clock TIME            set the clock now() reads (YYYY-MM-DDTHH:MM:SS[.mmm]Z or ms; default 0)
    dump                  print all globals
    host_timers           (anywhere) the host runs the timers: start/stop requests are
                          traced and `tick` does nothing
    fire TIMER            host timers: the host's timer fired, then run until idle

Values are written by type: int 12, float 2.5 (also 1e-07, nan, inf), str "a b\n"
(quoted, escapes \" \\ \n \t \xNN), time 2026-11-01T12:00:00.000Z (or ms in a scenario).

Trace lines:
    call NAME(a,b) -> r                             (call NAME(a,b) when it returns nothing)
    emit NAME(a,b)
    state NAME
    fault CODE at PC
    global[i] = v
    timer start|every NAME MS / timer stop NAME     (host_timers only)
    fire NAME ignored                               (timer was stopped meanwhile)
"""

from .vm import VM, IDLE
from . import values as V

WS = " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f"


def split_words(line):
    """Words separated by whitespace; a word starting with '"' runs to the closing quote
    (backslash escapes the next character). '#' outside quotes starts a comment."""
    words = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in WS:
            i += 1
            continue
        if c == "#":
            break
        j = i
        if c == '"':
            j += 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == "\\" else 1
            j = min(j + 1, n)
        while j < n and line[j] not in WS and line[j] != "#":
            j += 1
        words.append(line[i:j])
        i = j
    return words


def parse_scenario(text):
    cmds = []
    for raw in text.splitlines():
        parts = split_words(raw)
        if not parts:
            continue
        cmds.append((parts[0], parts[1:]))
    return cmds


def show_args(args):
    return ",".join(V.show(a) for a in args)


def run_scenario(prog, scenario_text, out, record=None):
    """Run a scenario, writing the trace with out(line).  record: a sink for the recording."""
    stubs = {}
    clock = [0]

    def make_stub(name, ret):
        def stub(vm, args):
            if ret == V.NONE_TYPE:
                out("call %s(%s)" % (name, show_args(args)))
                return 0
            r = stubs.get(name, V.zero(ret))
            out("call %s(%s) -> %s" % (name, show_args(args), V.show(r)))
            if name in stubs and isinstance(r, V.Str):
                return r.b              # a host string: the VM copies it into a slot
            return r
        return stub

    externs = {e[0]: make_stub(e[0], e[3]) for e in prog.externs}
    ext_types = {e[0]: e[3] for e in prog.externs}

    def on_emit(vm, name, args):
        out("emit %s(%s)" % (name, show_args(args)))

    def on_log(vm, level, template, values):
        out("log %s %s" % (V.LOG_LEVELS[level], V.quote(V.format_log(template, values))))

    def on_fault(vm, code, pc):
        out("fault %d at %d" % (code, pc))

    def on_timer(vm, i, start, ms, periodic):
        name = prog.events[prog.timers[i]][0]
        if start:
            out("timer %s %s %d" % ("every" if periodic else "start", name, ms))
        else:
            out("timer stop %s" % name)

    cmds = parse_scenario(scenario_text)
    host_timers = any(c == "host_timers" for c, _a in cmds)

    vm = VM(prog, externs, on_emit=on_emit, on_fault=on_fault, on_log=on_log,
            on_timer=on_timer if host_timers else None, clock=lambda vm: clock[0], record=record)
    snap = [None]
    last_state = [None]

    def run():
        while True:
            r = vm.step()
            if vm.state != last_state[0]:
                last_state[0] = vm.state
                out("state %s" % prog.states[vm.state])
            if r == IDLE:
                break

    def dump():
        for i, v in enumerate(vm.globals):
            out("global[%d] = %s" % (i, V.show(v)))

    vm.start()
    run()
    for cmd, args in cmds:
        if cmd == "extern":
            t = ext_types.get(args[0])
            if t is not None and t != V.NONE_TYPE:
                stubs[args[0]] = V.parse_literal(t, args[1])
        elif cmd == "post":
            ev = prog.event_index(args[0])
            if ev < 0:
                raise ValueError("unknown event %r" % args[0])
            ptypes = prog.events[ev][2]
            vals = [V.parse_literal(t, a) for t, a in zip(ptypes, args[1:])]
            vm.post(ev, *[v.b if isinstance(v, V.Str) else v for v in vals])
            run()
        elif cmd == "tick":
            ms = int(args[0], 0)
            if not -2 ** 31 <= ms < 2 ** 31:
                raise ValueError("tick value out of int32 range")
            clock[0] = V.wrap64(clock[0] + ms)
            vm.tick(ms)
            run()
        elif cmd == "clock":
            clock[0] = V.parse_literal(V.TIME, args[0]).ms
        elif cmd == "dump":
            dump()
        elif cmd == "host_timers":
            pass
        elif cmd == "snapshot":
            snap[0] = vm.snapshot()
            out("snapshot " + snap[0].hex())
        elif cmd == "restore":
            vm.restore(snap[0])
            last_state[0] = vm.state
            out("restored")
        elif cmd == "keyframe":
            vm.record_keyframe()
        elif cmd == "fire":
            idx = vm.timer_index(args[0])
            if vm.timers[idx][0]:
                vm.timer_expired(idx)
            else:
                out("fire %s ignored" % args[0])
            run()
        else:
            raise ValueError("unknown scenario command %r" % cmd)
    vm.record_flush()
    dump()
    return vm


def replay_recording(prog, data, out, on_state=None):
    """Replay a recording, writing the trace lines the program produces (as run_scenario
    does for the same run, without the host's own lines: dump, snapshot, restored ...).
    data: bytes, or an iterable of byte chunks (a stream: lines come as frames arrive).
    on_state(name): called on every state change (e.g. to redraw a chart)."""
    from .record import Replay

    def on_extern(name, args, result):
        if result is None:
            out("call %s(%s)" % (name, show_args(args)))
        elif isinstance(result, Exception):
            if getattr(result, "value", None) is not None:      # a refused str result
                out("call %s(%s) -> %s" % (name, show_args(args), V.show(result.value)))
        else:
            out("call %s(%s) -> %s" % (name, show_args(args), V.show(result)))

    def on_timer(vm, i, start, ms, periodic):
        name = prog.events[prog.timers[i]][0]
        out("timer %s %s %d" % ("every" if periodic else "start", name, ms) if start else "timer stop %s" % name)

    last_state = [None]

    def on_step(_r):
        if rp.vm.state != last_state[0]:
            last_state[0] = rp.vm.state
            out("state %s" % prog.states[rp.vm.state])
            if on_state:
                on_state(prog.states[rp.vm.state])

    rp = Replay(prog, on_emit=lambda vm, n, a: out("emit %s(%s)" % (n, show_args(a))),
                on_fault=lambda vm, c, pc: out("fault %d at %d" % (c, pc)),
                on_log=lambda vm, lv, t, vals: out("log %s %s" % (V.LOG_LEVELS[lv], V.quote(V.format_log(t, vals)))),
                on_timer=on_timer, on_extern=on_extern, on_step=on_step)
    for chunk in ([data] if isinstance(data, (bytes, bytearray)) else data):
        rp.feed(chunk)
    for i, v in enumerate(rp.vm.globals):
        out("global[%d] = %s" % (i, V.show(v)))
    return rp
