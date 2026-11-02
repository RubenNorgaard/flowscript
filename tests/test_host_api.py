"""The host-facing API (flowscript.compile / load / Machine) and the SVG renderer (to_svg)."""
import datetime as dt
import glob
import io
import os
import sys
import tempfile
import unittest
import xml.dom.minidom

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flowscript
from flowscript import CompileError, Machine

ROOT = os.path.join(os.path.dirname(__file__), "..")
PROGRAMS = sorted(glob.glob(os.path.join(ROOT, "examples", "*.fs")) +
                  glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs")))
UTC = dt.timezone.utc

SRC = """
flow demo version 2
extern scale(float x) -> float
extern label(str prefix, int n) -> str
extern say(str text)
event ping(int n)
event when(time t)
timer beat
float total = 0
str last = ""
time seen

initial state idle:
  on boot:
    start beat every 1s

  on beat:
    total = total + scale(1)

  on when(t):
    seen = t

  on ping(n):
    last = label("n=", n)
    say(last)
    if n > 1:
      goto_state busy

state busy:
  int pings = 0

  # same period: the beat keeps running across the transition
  on enter:
    start beat every 1s

  on beat:
    total = total + scale(1)

  on when(t):
    seen = t

  on ping(n):
    pings = pings + 1
    raise_event when(now())
    if n == 0:
      goto_state idle
"""


def machine(**kw):
    said = []
    externs = {"scale": lambda x: x * 2.5, "label": lambda p, n: "%s%d" % (p, n), "say": said.append}
    m = Machine(flowscript.compile(SRC), externs=externs, **kw)
    m.said = said
    return m


class Compile(unittest.TestCase):
    def test_program_keeps_source_ast_and_warnings(self):
        p = flowscript.compile(SRC)
        self.assertEqual((p.name, p.version), ("demo", 2))
        self.assertEqual(p.source, SRC)
        self.assertEqual(p.ast["type"], "program")
        self.assertEqual(p.warnings, [])
        w = flowscript.compile("int x\nstate a:\n  on boot:\n    if x > 5:\n      pass\n").warnings
        self.assertEqual([d.line for d in w], [4])
        self.assertIn("magic number 5", w[0].message)

    def test_compile_error(self):
        with self.assertRaises(CompileError) as cm:
            flowscript.compile("state main:\n  on boot:\n    y = 1\n")
        self.assertEqual(cm.exception.line, 3)
        self.assertIn("unknown variable 'y'", str(cm.exception))
        self.assertIsInstance(cm.exception, flowscript.FlowError)

    def test_load_from_bytes_path_and_file(self):
        p = flowscript.compile(SRC)
        data = p.to_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.fsb")
            p.save(path)
            for src in (data, bytearray(data), memoryview(data), path, io.BytesIO(data)):
                q = flowscript.load(src)
                self.assertEqual(q.code, p.code)
        self.assertIsNone(q.ast)
        with self.assertRaises(ValueError):
            flowscript.load(b"not bytecode")
        with self.assertRaises(TypeError):
            flowscript.load(42)


class Run(unittest.TestCase):
    def test_typed_values_cross_as_python(self):
        m = machine().start()
        self.assertEqual(m.state, "idle")
        m.send("ping", 1)
        self.assertEqual(m["last"], "n=1")
        self.assertEqual(m.said, ["n=1"])
        self.assertIsInstance(m["seen"], dt.datetime)
        self.assertEqual(m["seen"], dt.datetime(1970, 1, 1, tzinfo=UTC))
        m.advance(3000)                                   # three beats
        self.assertEqual(m["total"], 7.5)
        when = dt.datetime(2026, 11, 1, 12, 30, 15, 250000, tzinfo=UTC)
        m.send("when", when)
        self.assertEqual(m["seen"], when)
        m["total"] = 1                                    # an int is accepted for a float
        self.assertEqual(m.globals["total"], 1.0)

    def test_states_events_clock_and_state_variables(self):
        events = []
        t = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        m = machine(clock=lambda: t.timestamp(), on_event=lambda n, a: events.append((n, a))).start()
        m.send("ping", 5)
        self.assertEqual(m.state, "busy")
        m.send("ping", 7)
        self.assertEqual(m["busy.pings"], 1)
        self.assertEqual(events, [("when", [t])])
        self.assertEqual(m["seen"], t)                    # the script handled its own event
        m.send("ping", 0)
        self.assertEqual(m.state, "idle")
        m.send("ping", 3)
        self.assertEqual(m["busy.pings"], 0)              # reset when busy was entered again
        self.assertTrue(m.idle)

    def test_clock_may_return_a_datetime(self):
        t = dt.datetime(2030, 5, 6, tzinfo=UTC)
        m = machine(clock=lambda: t).start()
        m.send("ping", 2)
        m.send("ping", 1)
        self.assertEqual(m["seen"], t)

    def test_host_timers(self):
        calls = []
        m = machine(on_timer=lambda *a: calls.append(a)).start()
        self.assertEqual(calls, [("beat", True, 1000, True)])
        m.advance(5000)                                   # host runs the timers: no effect
        self.assertEqual(m["total"], 0.0)
        self.assertTrue(m.timer_expired("beat"))
        self.assertEqual(m["total"], 2.5)

    def test_faults_are_reported_with_line_and_handler(self):
        faults = []
        src = "extern f() -> int\nint x\nstate main:\n  on boot:\n    x = 10 / f()\n    x = 99\n"
        m = Machine(flowscript.compile(src), externs={"f": lambda: 0}, on_fault=faults.append).start()
        self.assertEqual(m["x"], 0)                       # the handler was aborted
        self.assertEqual(len(faults), 1)
        f = faults[0]
        self.assertEqual((f.code, f.name, f.line, f.handler), (3, "division by zero", 5, "main.on boot"))
        self.assertEqual(m.faults, faults)

    def test_extern_checks(self):
        p = flowscript.compile(SRC)
        with self.assertRaisesRegex(ValueError, "no host function for extern.*say"):
            Machine(p, externs={"scale": abs, "label": str})
        with self.assertRaisesRegex(ValueError, "declares no extern.*nope"):
            Machine(p, externs={"scale": abs, "label": str, "say": print, "nope": print})

    def test_naive_datetime_is_refused(self):
        m = machine().start()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            m.post("when", dt.datetime(2026, 1, 1))

    def test_bytecode_runs_like_the_source(self):
        p = flowscript.compile(SRC)
        externs = {"scale": lambda x: x * 2.5, "label": lambda p, n: "%s%d" % (p, n), "say": lambda t: None}
        a = Machine(p, externs=externs).start()
        b = Machine(p.to_bytes(), externs=externs).start()
        with_names = Machine(flowscript.load(p.to_bytes(debug=True)), externs=externs).start()
        for m in (a, b, with_names):
            m.send("ping", 2)
            m.advance(2000)
        self.assertEqual(a.vm.globals, b.vm.globals)
        self.assertEqual(with_names["total"], 5.0)
        with self.assertRaisesRegex(KeyError, "no global names"):
            b["total"]


class Migrate(unittest.TestCase):
    OLD = """event e
event f
timer t
int x
any_state:
  # shared
  on boot:
    start t every 1s

  on e:
    x = 1

  on f:
    x = 2

initial state a:
  on e:
    x = 10

state b:
  pass
"""

    def test_any_state_is_rewritten(self):
        new, notes = flowscript.migrate(self.OLD)
        p = flowscript.compile(new)                     # valid 0.2
        self.assertNotIn("any_state", new)
        ast = p.ast
        a, b = [i for i in ast["items"] if i["type"] == "state"]
        self.assertEqual([h["event"] for h in a["handlers"] if h["type"] == "on"], ["boot", "e", "f"])
        self.assertEqual([h["event"] for h in b["handlers"] if h["type"] == "on"], ["e", "f"])
        self.assertIn("shared", a["handlers"][0]["note"])     # the comment moved with 'on boot'
        # a's own 'on e' stays (it overrode the shared one); b got the shared one
        m = Machine(p).start()
        m.send("e")
        self.assertEqual(m["x"], 10)
        self.assertTrue(any("timer 't'" in n for n in notes))
        self.assertTrue(any("'on f' is now in a, b" in n for n in notes))

    def test_without_states_and_already_migrated(self):
        new, notes = flowscript.migrate("int x\nany_state:\n  on boot:\n    x = 1\n")
        self.assertIn("state main:", new)
        self.assertEqual(Machine(flowscript.compile(new)).start()["x"], 1)
        self.assertEqual(notes, [])
        self.assertEqual(flowscript.migrate(new), (new, []))


class Svg(unittest.TestCase):
    def test_every_program_draws_valid_svg(self):
        for fn in PROGRAMS:
            with open(fn) as f:
                src = f.read()
            for view in ("flow", "states"):
                with self.subTest(fn=os.path.basename(fn), view=view):
                    svg = flowscript.to_svg(src, view)
                    doc = xml.dom.minidom.parseString(svg)
                    self.assertEqual(doc.documentElement.tagName, "svg")
                    self.assertNotIn("<script", svg)

    def test_inputs_and_options(self):
        p = flowscript.compile(SRC)
        self.assertEqual(flowscript.to_svg(p), flowscript.to_svg(SRC))
        self.assertEqual(flowscript.to_svg(p.ast), flowscript.to_svg(SRC))
        cur = flowscript.to_svg(p, state="busy")
        self.assertIn('class="state cur" data-state="busy"', cur)
        self.assertIn("\u25c0 current", cur)
        self.assertIn('class="nd sd-state cur" data-state="busy"', flowscript.to_svg(p, "states", state="busy"))
        self.assertIn('width="100%"', flowscript.to_svg(p, width="100%"))
        self.assertIn("prefers-color-scheme", flowscript.to_svg(p))
        self.assertNotIn("prefers-color-scheme", flowscript.to_svg(p, theme="light"))
        self.assertIn("aria-label=\"flow demo\"", flowscript.to_svg(p))
        with self.assertRaises(TypeError):
            flowscript.to_svg(flowscript.load(p.to_bytes()))
        with self.assertRaises(ValueError):
            flowscript.to_svg(p, view="nope")

    def test_ids_are_unique_per_chart(self):
        a = flowscript.to_svg(SRC)
        b = flowscript.to_svg(SRC, state="busy")
        c = flowscript.to_svg(SRC, "states")
        prefix = lambda s: s.split('marker id="')[1].split("-arr")[0]
        self.assertEqual(len({prefix(a), prefix(b), prefix(c)}), 3)
        self.assertEqual(prefix(a), prefix(flowscript.to_svg(SRC)))       # stable
        self.assertIn('id="x-arr"', flowscript.to_svg(SRC, id_prefix="x"))


if __name__ == "__main__":
    unittest.main()
