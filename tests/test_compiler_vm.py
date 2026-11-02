"""Compiler diagnostics and Python VM semantics."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flowscript import compile_source, FlowError, VM, Program
from flowscript.vm import IDLE


def run(src, events=(), externs=None):
    _ast, prog, _ = compile_source(src)
    trace = []
    vm = VM(prog, externs or {}, on_emit=lambda vm, n, a: trace.append(("emit", n, a)),
            on_fault=lambda vm, c, pc: trace.append(("fault", c)))
    vm.start()
    vm.run()
    for ev in events:
        if isinstance(ev, int):
            vm.tick(ev)
        else:
            vm.post(*ev)
        vm.run()
    return vm, trace


def g(vm, name):
    return vm.globals[vm.prog.global_names.index(name)]


class Errors(unittest.TestCase):
    def bad(self, src, fragment):
        with self.assertRaises(FlowError) as cm:
            compile_source(src)
        self.assertIn(fragment, str(cm.exception))

    def test_messages(self):
        self.bad("state main:\n  on boot:\n    x = 1\n", "unknown variable 'x'")
        self.bad("const K = 1\nstate main:\n  on boot:\n    K = 2\n", "cannot assign to constant")
        self.bad("state main:\n  on nothing:\n    pass\n", "unknown event")
        self.bad("extern f(int a) -> int\nstate main:\n  on boot:\n    f()\n", "takes 1 argument")
        self.bad("state main:\n  on boot:\n    goto_state nowhere\n", "unknown state")
        self.bad("int a = 1\nint a = 2\n", "already declared")
        self.bad("state main:\n  on boot:\n    if 1 < 2 < 3:\n      pass\n", "cannot be chained")
        self.bad("state main:\n  on boot:\n    return 3\n", "cannot return a value")
        self.bad("state main:\n  on boot:\n    int x\n    int x\n", "already declared")
        self.bad("int v = 5\nconst C = v\n", "constant expression")
        self.bad("const C = 1 / 0\n", "division by zero")
        self.bad("state main:\n  on boot:\n    x = = 1\n", "expected an expression")

    def test_layout_messages(self):
        self.bad("state main:\n  on boot\n    pass\n", "expected ':'")
        self.bad("state main:\n  on boot:\n", "expected an indented block")
        self.bad("state main:\n  on boot:\npass\n", "expected an indented block")
        self.bad("state main:\n  on boot:\n    pass\n      pass\n", "unexpected indentation")
        self.bad("int x\nstate main:\n  on boot:\n      x = 1\n    x = 2\n", "does not match any outer block")
        self.bad("state main:\n  on boot:\n  \tpass\n", "tab used for indentation")
        self.bad("state main:\n  on boot:\n    pass\nend\n", "'end' is not used")
        self.bad("state main:\n  on boot:\n    pass\n    else:\n      pass\n", "without a matching 'if'")
        self.bad("state main:\n  on boot:\n    if true: pass\n", "unexpected 'pass'")
        self.bad("state a:\n  on enter:\n    goto a\n", "use 'goto_state a'")
        self.bad("event e\nstate main:\n  on boot:\n    emit e\n", "use 'raise_event e'")

    def test_any_indent_width(self):
        _ast, prog, _ = compile_source("int x\nstate main:\n  on boot:\n      if x:\n          x = 1\n      else:\n          x = 2\n")
        self.assertTrue(prog.code)

    def test_error_line(self):
        with self.assertRaises(FlowError) as cm:
            compile_source("state main:\n  on boot:\n\n    y = 1\n")
        self.assertEqual(cm.exception.line, 4)

    def test_states_only_rules(self):
        self.bad("event e\non e:\n  pass\n", "event handlers go inside a state")
        self.bad("event e\nany_state:\n  on e:\n    pass\n", "'any_state' was removed")
        self.bad("initial state a:\n  pass\nstate b:\n  on boot:\n    pass\n",
                 "'on boot' goes in the initial state ('a')")
        self.bad("state a:\n  int n\n  on boot:\n    n = 1\n", "'on boot' runs before 'a' is entered")
        self.bad("state main:\n  x = 1\n", "only variables (int/float/str/time) and 'on' handlers")
        self.bad("state any_state:\n  pass\n", "keyword")
        # 'on boot' in the initial state, also when it is not the first one
        _ast, prog, _ = compile_source("state a:\n  pass\ninitial state b:\n  on boot:\n    pass\n")
        self.assertEqual([h[0] for h in prog.handlers], [0])

    def test_log_statement_rules(self):
        self.bad('state s:\n  on boot:\n    info "x is {x"\n', "'{' without a closing '}'")
        self.bad('state s:\n  on boot:\n    info "x}"\n', "'}' without an opening '{'")
        self.bad('state s:\n  on boot:\n    info "x {}"\n', "empty '{}' in the message")
        self.bad('state s:\n  on boot:\n    info "{1 +}"\n', "in {1 +}:")
        self.bad('state s:\n  on boot:\n    info "{nope}"\n', "unknown name 'nope'")
        self.bad('func f():\n  pass\nstate s:\n  on boot:\n    info "{f()}"\n', "returns nothing")
        self.bad('state s:\n  on boot:\n    info "' + "{1}" * 9 + '"\n', "at most 8 values")
        self.bad('state s:\n  on boot:\n    error("oops")\n', 'to log, write error "message {value}"')
        # the level words stay usable as names; the error points at the statement's line
        logs = []
        prog = compile_source('int error = 1\nstate s:\n  on boot:\n    error = error + 1\n'
                              '    warning "error is {error}, {{}} {1.5 * 2}"\n')[1]
        vm = VM(prog, on_log=lambda vm, lv, t, vals: logs.append((lv, t, vals)))
        vm.start(); vm.run()
        self.assertEqual(logs, [(2, "error is {}, {{}} {}", [2, 3.0])])
        with self.assertRaises(FlowError) as cm:
            compile_source('state s:\n  on boot:\n    pass\n    info "{nope}"\n')
        self.assertEqual(cm.exception.line, 4)

    def test_several_results_rules(self):
        f = "func f(int x) -> int, float:\n  return x, 2.5\n"
        self.bad(f + "int a\nstate s:\n  on boot:\n    a = f(1)\n", "'f' returns 2 values: assign them with a, b = f(...)")
        self.bad(f + "int a\nstate s:\n  on boot:\n    a, a = f(1)\n", "'a' is assigned twice")
        self.bad(f + "int a\nint b\nint c\nstate s:\n  on boot:\n    a, b, c = f(1)\n",
                 "'f' returns 2 values, but 3 variables are assigned")
        self.bad(f + "int a\nint b\nstate s:\n  on boot:\n    a, b = f(1)\n",
                 "'b' is int, but result 2 of f() is float")
        self.bad(f + "int a\nint b\nstate s:\n  on boot:\n    a, b = 3\n", "can only be assigned from a function call")
        self.bad(f + "int a\nint b\nstate s:\n  on boot:\n    a, b = abs(1)\n", "only a func with several results")
        self.bad("func g() -> int, int:\n  return 1\n", "'g' returns 2 values: return ..., ...")
        self.bad("func g() -> int, int:\n  return 1, 2, 3\n", "'g' returns 2 values, not 3")
        self.bad("func g() -> int, str:\n  return 1, 2\n", "result 2 of 'g' is str")
        self.bad("func g() -> int:\n  return 1, 2\n", "'g' returns 1 value, not 2")
        self.bad("extern h() -> int, int\n", "a host function (extern) returns at most one value")
        self.bad("func g() -> " + ", ".join(["int"] * 9) + ":\n  pass\n", "at most 8 values")
        # an int result goes into a float variable; results feed the next call
        self.bad(f + "float a\nfloat b\nstate s:\n  on boot:\n    a, b = f(4)\n    a, b = f(a + 1)\n",
                 "argument 1 of f() (int)")
        vm, _ = run(f + "float a\nfloat b\nint n\nstate s:\n  on boot:\n    a, b = f(4)\n    n, b = f(7)\n")
        self.assertEqual((g(vm, "a"), g(vm, "b"), g(vm, "n")), (4.0, 2.5, 7))

    def test_state_var_rules(self):
        self.bad("event e\ninitial state a:\n  int n\n  on e:\n    goto_state b\nstate b:\n  on e:\n    n = 1\n",
                 "'n' is a variable of state 'a', only visible in that state's handlers")
        self.bad("func f() -> int:\n  return n\nstate a:\n  int n\n", "variable of state 'a'")
        self.bad("state a:\n  on enter:\n    pass\n  int n\n", "before its handlers")
        self.bad("state a:\n  int n\n  int n\n", "already declared in state 'a'")
        self.bad("int n\nstate a:\n  int n\n", "already declared as var")
        self.bad("state a:\n  int n\n  on enter:\n    int n = 2\n", "hide the state variable")
        self.bad("event e(int n)\nstate a:\n  int n\n  on e:\n    pass\n", "hide the state variable")
        self.bad("int g\nstate a:\n  int n = g\n", "constant expression")

    def test_initial_state_rules(self):
        self.bad("state a:\n  pass\nstate b:\n  pass\n", "mark the state the program starts in: 'initial state a:'")
        self.bad("initial state a:\n  pass\ninitial state b:\n  pass\n", "only one state can be 'initial'")
        self.bad("initial int x\n", "write 'initial state NAME:'")
        # a single state needs no marker; the marked state starts even when it is not first
        self.assertEqual(compile_source("state a:\n  pass\n")[1].states, ["a"])
        vm, _ = run("int x\nstate a:\n  on enter:\n    x = 1\ninitial state b:\n  on enter:\n    x = 2\n")
        self.assertEqual((vm.state_name(), g(vm, "x")), ("b", 2))

    def test_funcs_cannot_use_globals(self):
        msg = "functions cannot use global variables: pass 'g' in as a parameter"
        self.bad("int g\nfunc f() -> int:\n  return g\n", msg)
        self.bad("int g\nfunc f() -> int:\n  g = 1\n", msg)
        self.bad("int g\nfunc f(int n) -> int:\n  if n > g:\n    pass\n", msg)
        # parameters, locals, consts and calls are fine; handlers store the result
        _ast, prog, _ = compile_source("const K = 2\nint g\nfunc f(int n) -> int:\n  int t = n * K\n  return t\n"
                                       "state main:\n  on boot:\n    g = f(g + 1)\n")
        self.assertTrue(prog.code)

    def test_magic_number_warning(self):
        src = ("const LIMIT = 4\nint x\nfunc f(int n) -> int:\n  return n > 7\n"
               "state main:\n  on boot:\n    if x == 0 or x != -1 or x < 1 or x > LIMIT or x >= LIMIT / 2:\n"
               "      pass\n    while -300 < x:\n      x = x - 1\n")
        _ast, _prog, dbg = compile_source(src)
        self.assertEqual([(w["line"], w["msg"]) for w in dbg.warnings], [
            (4, "magic number 7 in 'n > 7': name it, e.g. 'const NAME = 7'"),
            (9, "magic number -300 in '-300 < x': name it, e.g. 'const NAME = -300'")])


class Semantics(unittest.TestCase):
    def test_arith(self):
        vm, _ = run("""
int a
int b
int c
int d
state main:
  on boot:
    a = -7 / 2
    b = -7 % 2
    c = 0x7fffffff + 1
    d = 1 << 33
""")
        self.assertEqual([g(vm, x) for x in "abcd"], [-3, -1, -2147483648, 2])

    def test_short_circuit(self):
        calls = []
        def f(vm, args):
            calls.append(args[0])
            return args[0]
        vm, _ = run("""
extern f(int x) -> int
int r
state main:
  on boot:
    r = f(0) and f(1)
    r = r + (f(2) or f(3)) * 10
    r = r + (f(0) xor f(4)) * 100
""", externs={"f": f})
        self.assertEqual(calls, [0, 2, 0, 4])
        self.assertEqual(g(vm, "r"), 110)

    def test_states_and_timers(self):
        vm, trace = run("""
event go
timer t
int log = 0
initial state a:
  on enter:
    log = log * 10 + 1
    start t every 100
  on t:
    log = log * 10 + 2
  on go:
    goto_state b
  on exit:
    log = log * 10 + 3
    goto_state a
state b:
  on enter:
    log = log * 10 + 4
    stop t
""", events=[100, 250, ("go",), 1000])
        self.assertEqual(g(vm, "log"), 12234)
        self.assertEqual(vm.state_name(), "b")

    def test_state_vars_reset_on_entry(self):
        src = """
event go
event bump
int seen = 0
initial state a:
  int n = 5
  on bump:
    n = n + 1
    seen = n
  on go:
    goto_state b
state b:
  int n = 100
  on enter:
    seen = n
  on go:
    goto_state a
"""
        vm, _ = run(src, events=[("bump",), ("bump",)])
        self.assertEqual(g(vm, "seen"), 7)
        self.assertEqual(g(vm, "a.n"), 7)
        vm, _ = run(src, events=[("bump",), ("go",)])
        self.assertEqual(g(vm, "seen"), 100)      # b's own 'on enter' sees the reset value
        vm, _ = run(src, events=[("bump",), ("bump",), ("go",), ("go",), ("bump",)])
        self.assertEqual(g(vm, "seen"), 6)        # a was re-entered: n started again at 5

    def test_boot_selects_state_and_override(self):
        vm, trace = run("""
event e
int x
initial state first:
  on boot:
    goto_state second
  on enter:
    x = 99
  on e:
    x = 1
state second:
  on e:
    x = x + 2
    if x < 6:
      raise_event e
""", events=[("e",)])
        self.assertEqual(vm.state_name(), "second")       # first was never entered
        self.assertEqual(g(vm, "x"), 6)

    def test_faults(self):
        vm, trace = run("""
int z
state main:
  on boot:
    z = 1 / z
""")
        self.assertEqual(trace, [("fault", 3)])
        vm, trace = run("state main:\n  on boot:\n    while true:\n      pass\n")
        self.assertEqual(trace, [("fault", 4)])
        vm, trace = run("func f(int n) -> int:\n  return f(n + 1)\nstate main:\n  on boot:\n    f(0)\n")
        self.assertEqual(trace, [("fault", 6)])

    def test_livelock_is_stopped(self):
        for src in ("initial state a:\n  on enter:\n    goto_state b\nstate b:\n  on enter:\n    goto_state a\n",
                    "event e\nstate main:\n  on boot:\n    raise_event e\n  on e:\n    raise_event e\n"):
            vm, trace = run(src)
            self.assertEqual([t for t in trace if t[0] == "fault"], [("fault", 9)])
            self.assertEqual(vm.step(), IDLE)

    def test_functions_and_locals(self):
        vm, _ = run("""
int r
func fact(int n) -> int:
  if n <= 1:
    return 1
  return n * fact(n - 1)
state main:
  on boot:
    int i = 0
    while i < 5:
      i = i + 1
    r = fact(i)
""")
        self.assertEqual(g(vm, "r"), 120)

    def test_bytecode_roundtrip(self):
        _ast, prog, _ = compile_source(open(os.path.join(os.path.dirname(__file__), "..", "examples", "gps_tracker.fs")).read())
        for dbg in (False, True):
            p2 = Program.from_bytes(prog.to_bytes(debug=dbg))
            self.assertEqual(p2.code, prog.code)
            self.assertEqual(p2.handlers, prog.handlers)
            self.assertEqual(p2.funcs, prog.funcs)
            self.assertEqual(p2.name, "tracker")


if __name__ == "__main__":
    unittest.main()


class Rejected(unittest.TestCase):
    """Programs in tests/reject/ exceed VM limits and must not compile."""
    def test_reject(self):
        import glob
        for fn in glob.glob(os.path.join(os.path.dirname(__file__), "reject", "*.fs")):
            with self.subTest(fn=fn), self.assertRaises(FlowError):
                compile_source(open(fn).read())

    def test_corrupt_image(self):
        _ast, prog, _ = compile_source("state main:\n  on boot:\n    pass\n")
        data = prog.to_bytes()
        for n in range(len(data)):
            try:
                Program.from_bytes(data[:n])
            except ValueError:
                pass

    def test_truncated_operand_faults(self):
        _ast, prog, _ = compile_source("int x\nstate main:\n  on boot:\n    x = 1000\n")
        prog.code = prog.code[:2]           # PUSH with a cut-off operand
        faults = []
        vm = VM(prog, on_fault=lambda vm, c, pc: faults.append((c, pc)))
        vm.start(); vm.run()
        self.assertEqual(faults, [(5, 0)])

    def test_boot_fault_still_enters_initial_state(self):
        vm, trace = run("int z\nstate a:\n  on boot:\n    z = 1 / z\n")
        self.assertEqual(trace, [("fault", 3)])
        self.assertEqual(vm.state_name(), "a")


class Types(unittest.TestCase):
    """Static types: int, float, str, time."""

    def bad(self, src, fragment):
        with self.assertRaises(FlowError) as cm:
            compile_source(src)
        self.assertIn(fragment, str(cm.exception))

    def boot(self, body, decls=""):
        return decls + "state main:\n  on boot:\n" + "".join("    %s\n" % l for l in body.split("\n"))

    def test_type_errors(self):
        self.bad(self.boot("int x = 1.5"), "'x' is int: got float, expected int")
        self.bad(self.boot('float f = "a"'), "got str, expected float")
        self.bad(self.boot("int x = 1 + \"a\""), "cannot use '+' on int and str")
        self.bad(self.boot("if 1.5:\n  pass"), "a condition must be an int")
        self.bad(self.boot('if "a" and 1:\n  pass'), "'and' needs conditions (int), not str")
        self.bad(self.boot("time t = now() + 1.5"), "cannot use '+' on time and float")
        self.bad(self.boot("int x = ~1.5"), "'~' needs an int operand")
        self.bad(self.boot("int x = len(5)"), "no len() takes (int)")
        self.bad(self.boot("int x = f()", "extern f()\n"), "'f' returns nothing")
        self.bad(self.boot("start t 1.5", "timer t\n"), "the timer duration (ms): got float")
        self.bad(self.boot("raise_event e(1)", "event e(str s)\n"), "e(str s): got int, expected str")
        self.bad("func f() -> int:\n  return\n", "must return a int value")
        self.bad("func f():\n  return 1\n", "'f' returns nothing: declare its type")
        self.bad("func f(x) -> int:\n  return x\n", "give the parameter a type: 'int x'")
        self.bad("var x = 1\n", "declare variables with their type: 'int x'")
        self.bad("const T = time(2026, 2, 30)\n", "invalid value in constant expression 'time'")
        self.bad("str s = 5\n", "the initial value of 's' is int, expected str")
        self.bad(self.boot("int x = 1.5s"), "time units need an integer")

    def test_void_call_statement_compiles(self):
        compile_source(self.boot("f()", "extern f()\n"))

    def test_promotion_and_folding(self):
        _ast, prog, _ = compile_source(
            "const HALF = 1 / 2.0\nconst T = time(\"2026-11-01T12:00:00Z\") + 1000\n"
            "const S = \"n=\" + str(3 * 4)\nfloat f = 3\nfloat g = HALF * 4\ntime t = T\nstr s = S\n")
        from flowscript.values import Str, Time, make_time
        self.assertEqual(prog.globals[:4], [3.0, 2.0, Time(make_time(2026, 11, 1, 12).ms + 1000), Str(b"n=12")])

    def test_semantics(self):
        got = []
        src = ('extern out(str s)\nevent go(float x, str s)\nfloat total = 0\n'
               'func avg(float a, float b) -> float:\n  return (a + b) / 2\n'
               'state main:\n  on go(x, s):\n    total = total + avg(x, 1)\n'
               '    if x > 2:\n      str extra = "!" + s\n    out(s + str(total) + extra)\n')
        vm, trace = run(src, [("go", 3, "a"), ("go", 0.5, "b")],
                        {"out": lambda vm, a: got.append(a[0].text)})
        self.assertEqual(got, ["a2.0!a", "b2.75"])     # 'extra' starts as "" when its branch is skipped
        self.assertEqual(trace, [])

    def test_strings_are_collected(self):
        src = ("event go(int n)\nstr keep\nstate main:\n  on go(n):\n    while n > 0:\n"
               "      keep = str(n) + \"-\" + str(n)\n      n = n - 1\n")
        vm, trace = run(src, [("go", 500)])
        self.assertEqual(trace, [])
        self.assertEqual(vm.globals[0].text, "1-1")
        self.assertLessEqual(vm.strings_in_use(), 16)  # garbage is reclaimed when the slots run out
        vm.collect()
        self.assertEqual(vm.strings_in_use(), 1)

    def test_host_values(self):
        src = "event e(float x, str s, time t)\nfloat x\nstr s\ntime t\nstate main:\n  on e(a, b, c):\n    x = a\n    s = b\n    t = c\n"
        from flowscript.values import Time
        vm, trace = run(src, [("e", 2, "hi", Time(5))])
        self.assertEqual((vm.globals[0], vm.globals[1].text, vm.globals[2]), (2.0, "hi", Time(5)))
        with self.assertRaises(TypeError):
            vm.post("e", "no", "x", Time(0))
        vm.post("e", 1.0, "x" * 65, Time(0))          # too long: fault 12, not queued
        self.assertEqual(len(vm.queue), 0)

    def test_clock(self):
        src = "time t\nstate main:\n  on boot:\n    t = now()\n"
        _ast, prog, _ = compile_source(src)
        vm = VM(prog, clock=lambda vm: 1793534400000)
        vm.start()
        vm.run()
        self.assertEqual(vm.globals[0].ms, 1793534400000)
