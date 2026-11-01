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
        self.bad("any_state:\n  on boot:\n    x = 1\n", "unknown variable 'x'")
        self.bad("const K = 1\nany_state:\n  on boot:\n    K = 2\n", "cannot assign to constant")
        self.bad("any_state:\n  on nothing:\n    pass\n", "unknown event")
        self.bad("any_state:\n  on enter:\n    pass\n", "only allowed inside a state")
        self.bad("extern f(a)\nany_state:\n  on boot:\n    f()\n", "takes 1 argument")
        self.bad("any_state:\n  on boot:\n    goto_state nowhere\n", "unknown state")
        self.bad("var a = 1\nvar a = 2\n", "already declared")
        self.bad("any_state:\n  on boot:\n    if 1 < 2 < 3:\n      pass\n", "cannot be chained")
        self.bad("any_state:\n  on boot:\n    return 3\n", "cannot return a value")
        self.bad("any_state:\n  on boot:\n    var x\n    var x\n", "already declared")
        self.bad("var v = 5\nconst C = v\n", "constant expression")
        self.bad("const C = 1 / 0\n", "division by zero")
        self.bad("any_state:\n  on boot:\n    x = = 1\n", "expected an expression")

    def test_layout_messages(self):
        self.bad("any_state:\n  on boot\n    pass\n", "expected ':'")
        self.bad("any_state:\n  on boot:\n", "expected an indented block")
        self.bad("any_state:\n  on boot:\npass\n", "expected an indented block")
        self.bad("any_state:\n  on boot:\n    pass\n      pass\n", "unexpected indentation")
        self.bad("var x\nany_state:\n  on boot:\n      x = 1\n    x = 2\n", "does not match any outer block")
        self.bad("any_state:\n  on boot:\n  \tpass\n", "tab used for indentation")
        self.bad("any_state:\n  on boot:\n    pass\nend\n", "'end' is not used")
        self.bad("any_state:\n  on boot:\n    pass\n    else:\n      pass\n", "without a matching 'if'")
        self.bad("any_state:\n  on boot:\n    if true: pass\n", "unexpected 'pass'")
        self.bad("state a:\n  on enter:\n    goto a\n", "use 'goto_state a'")
        self.bad("event e\nany_state:\n  on boot:\n    emit e\n", "use 'raise_event e'")

    def test_any_indent_width(self):
        _ast, prog, _ = compile_source("var x\nany_state:\n  on boot:\n      if x:\n          x = 1\n      else:\n          x = 2\n")
        self.assertTrue(prog.code)

    def test_error_line(self):
        with self.assertRaises(FlowError) as cm:
            compile_source("any_state:\n  on boot:\n\n    y = 1\n")
        self.assertEqual(cm.exception.line, 4)

    def test_any_state_rules(self):
        self.bad("event e\non e:\n  pass\n", "event handlers go inside a state, or inside 'any_state:'")
        self.bad("any_state:\n  pass\nany_state:\n  pass\n", "only one 'any_state' block")
        self.bad("state a:\n  on boot:\n    pass\n", "'on boot' belongs in any_state")
        self.bad("any_state:\n  on exit:\n    pass\n", "any_state is never entered or left")
        self.bad("any_state:\n  x = 1\n", "only 'on' handlers are allowed inside any_state")
        self.bad("state any_state:\n  pass\n", "keyword")

    def test_state_var_rules(self):
        self.bad("event e\ninitial state a:\n  var n\n  on e:\n    goto_state b\nstate b:\n  on e:\n    n = 1\n",
                 "'n' is a variable of state 'a', only visible in that state's handlers")
        self.bad("func f():\n  return n\nstate a:\n  var n\n", "variable of state 'a'")
        self.bad("any_state:\n  var n\n", "declare them at the top level")
        self.bad("state a:\n  on enter:\n    pass\n  var n\n", "before its handlers")
        self.bad("state a:\n  var n\n  var n\n", "already declared in state 'a'")
        self.bad("var n\nstate a:\n  var n\n", "already declared as var")
        self.bad("state a:\n  var n\n  on enter:\n    var n = 2\n", "hide the state variable")
        self.bad("event e(n)\nstate a:\n  var n\n  on e:\n    pass\n", "hide the state variable")
        self.bad("var g\nstate a:\n  var n = g\n", "constant expression")

    def test_initial_state_rules(self):
        self.bad("state a:\n  pass\nstate b:\n  pass\n", "mark the state the program starts in: 'initial state a:'")
        self.bad("initial state a:\n  pass\ninitial state b:\n  pass\n", "only one state can be 'initial'")
        self.bad("initial var x\n", "write 'initial state NAME:'")
        # a single state needs no marker; the marked state starts even when it is not first
        self.assertEqual(compile_source("state a:\n  pass\n")[1].states, ["a"])
        vm, _ = run("var x\nstate a:\n  on enter:\n    x = 1\ninitial state b:\n  on enter:\n    x = 2\n")
        self.assertEqual((vm.state_name(), g(vm, "x")), ("b", 2))

    def test_funcs_cannot_use_globals(self):
        msg = "functions cannot use global variables: pass 'g' in as a parameter"
        self.bad("var g\nfunc f():\n  return g\n", msg)
        self.bad("var g\nfunc f():\n  g = 1\n", msg)
        self.bad("var g\nfunc f(n):\n  if n > g:\n    pass\n", msg)
        # parameters, locals, consts and calls are fine; handlers store the result
        _ast, prog, _ = compile_source("const K = 2\nvar g\nfunc f(n):\n  var t = n * K\n  return t\n"
                                       "any_state:\n  on boot:\n    g = f(g + 1)\n")
        self.assertTrue(prog.code)

    def test_magic_number_warning(self):
        src = ("const LIMIT = 4\nvar x\nfunc f(n):\n  return n > 7\n"
               "any_state:\n  on boot:\n    if x == 0 or x != -1 or x < 1 or x > LIMIT or x >= LIMIT / 2:\n"
               "      pass\n    while -300 < x:\n      x = x - 1\n")
        _ast, _prog, dbg = compile_source(src)
        self.assertEqual([(w["line"], w["msg"]) for w in dbg.warnings], [
            (4, "magic number 7 in 'n > 7': name it, e.g. 'const NAME = 7'"),
            (9, "magic number -300 in '-300 < x': name it, e.g. 'const NAME = -300'")])

    def test_shadow_warning(self):
        src = ("event e\nany_state:\n  on e:\n    pass\n"
               "initial state a:\n  on e:\n    pass\nstate b:\n  pass\n")
        _ast, _prog, dbg = compile_source(src)
        self.assertEqual(len(dbg.warnings), 1)
        w = dbg.warnings[0]
        self.assertEqual(w["line"], 6)
        self.assertIn("in state 'a', 'on e' replaces the any_state handler (line 3)", w["msg"])
        _ast, _prog, dbg = compile_source("event e\nany_state:\n  on e:\n    pass\nstate a:\n  pass\n")
        self.assertEqual(dbg.warnings, [])


class Semantics(unittest.TestCase):
    def test_arith(self):
        vm, _ = run("""
var a
var b
var c
var d
any_state:
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
extern f(x)
var r
any_state:
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
var log = 0
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
var seen = 0
initial state a:
  var n = 5
  on bump:
    n = n + 1
    seen = n
  on go:
    goto_state b
state b:
  var n = 100
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
var x
any_state:
  on boot:
    goto_state second
  on e:
    x = 1
initial state first:
  on enter:
    x = 99
state second:
  on e:
    x = 2
    raise_event e
""", events=[("e",)])
        self.assertEqual(vm.state_name(), "second")
        self.assertEqual(g(vm, "x"), 2)

    def test_faults(self):
        vm, trace = run("""
var z
any_state:
  on boot:
    z = 1 / z
""")
        self.assertEqual(trace, [("fault", 3)])
        vm, trace = run("any_state:\n  on boot:\n    while true:\n      pass\n")
        self.assertEqual(trace, [("fault", 4)])
        vm, trace = run("func f(n):\n  return f(n + 1)\nany_state:\n  on boot:\n    f(0)\n")
        self.assertEqual(trace, [("fault", 6)])

    def test_livelock_is_stopped(self):
        for src in ("initial state a:\n  on enter:\n    goto_state b\nstate b:\n  on enter:\n    goto_state a\n",
                    "event e\nany_state:\n  on boot:\n    raise_event e\n  on e:\n    raise_event e\n"):
            vm, trace = run(src)
            self.assertEqual([t for t in trace if t[0] == "fault"], [("fault", 9)])
            self.assertEqual(vm.step(), IDLE)

    def test_functions_and_locals(self):
        vm, _ = run("""
var r
func fact(n):
  if n <= 1:
    return 1
  return n * fact(n - 1)
any_state:
  on boot:
    var i = 0
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
        _ast, prog, _ = compile_source("any_state:\n  on boot:\n    pass\n")
        data = prog.to_bytes()
        for n in range(len(data)):
            try:
                Program.from_bytes(data[:n])
            except ValueError:
                pass

    def test_truncated_operand_faults(self):
        _ast, prog, _ = compile_source("var x\nany_state:\n  on boot:\n    x = 1000\n")
        prog.code = prog.code[:2]           # PUSH with a cut-off operand
        faults = []
        vm = VM(prog, on_fault=lambda vm, c, pc: faults.append((c, pc)))
        vm.start(); vm.run()
        self.assertEqual(faults, [(5, 0)])

    def test_boot_fault_still_enters_initial_state(self):
        vm, trace = run("var z\nany_state:\n  on boot:\n    z = 1 / z\nstate a:\n  pass\n")
        self.assertEqual(trace, [("fault", 3)])
        self.assertEqual(vm.state_name(), "a")
