"""Scenario runner with a canonical trace format.

The C runner (c/fsrun.c) implements the exact same format; the conformance test
compares the two traces line by line.

Scenario file (one command per line, '#' comments):
    extern NAME VALUE     set the return value of a stubbed extern (default 0)
    post EVENT [ARGS...]  queue an event, then run until idle
    tick MS               advance timers, then run until idle
    dump                  print all globals
    host_timers           (anywhere) the host runs the timers: start/stop requests are
                          traced and `tick` does nothing
    fire TIMER            host timers: the host's timer fired, then run until idle

Trace lines:
    call NAME(a,b) -> r
    emit NAME(a,b)
    state NAME
    fault CODE at PC
    global[i] = v
    timer start|every NAME MS / timer stop NAME     (host_timers only)
    fire NAME ignored                               (timer was stopped meanwhile)
"""

from .vm import VM, IDLE


def parse_scenario(text):
    cmds = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmds.append((parts[0], parts[1:]))
    return cmds


def run_scenario(prog, scenario_text, out):
    stubs = {}

    def make_stub(name):
        def stub(vm, args):
            r = stubs.get(name, 0)
            out("call %s(%s) -> %d" % (name, ",".join(str(a) for a in args), r))
            return r
        return stub

    externs = {name: make_stub(name) for name, _ in prog.externs}

    def on_emit(vm, name, args):
        out("emit %s(%s)" % (name, ",".join(str(a) for a in args)))

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

    vm = VM(prog, externs, on_emit=on_emit, on_fault=on_fault,
            on_timer=on_timer if host_timers else None)
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
            out("global[%d] = %d" % (i, v))

    vm.start()
    run()
    for cmd, args in cmds:
        if cmd == "extern":
            stubs[args[0]] = int(args[1], 0)
        elif cmd == "post":
            vm.post(args[0], *[int(a, 0) for a in args[1:]])
            run()
        elif cmd == "tick":
            vm.tick(int(args[0], 0))
            run()
        elif cmd == "dump":
            dump()
        elif cmd == "host_timers":
            pass
        elif cmd == "fire":
            idx = vm.timer_index(args[0])
            if vm.timers[idx][0]:
                vm.timer_expired(idx)
            else:
                out("fire %s ignored" % args[0])
            run()
        else:
            raise ValueError("unknown scenario command %r" % cmd)
    dump()
    return vm
