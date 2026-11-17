"""Monitor mode (SPEC 10.5): the observation stream, read by Monitor and flowscript view.

The C VM writes the same bytes (tests/conformance.py); here: what the stream says, that a
viewer names it from any build of the same declarations, and the numbering rule."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import flowscript
from flowscript import Machine, Monitor
from flowscript.observe import OBS_TAGS, MonitorError, Refused, is_observation
from flowscript.record import read_frames

SRC = """flow lamp version 2

extern dim(int level) -> int
extern label() -> str
event press(int n, str who)
event wobble(float x)
const MAX = 3
int presses = 0
str last = ""
float wob = 0.0

initial state off:
  on press(n, who):
    presses = presses + n
    last = who
    info "press {n} by {who}"
    if presses >= MAX:
      goto_state lit

  on wobble(x):
    wob = x

state lit:
  int level = 0

  on enter:
    level = dim(5)
    raise_event wobble(1.5)

  on press(n, who):
    last = label()
    presses = 0
    goto_state off

  on wobble(x):
    wob = x
"""


def tags(frames):
    return [OBS_TAGS[f[1]] for f in frames]


class Observe(unittest.TestCase):
    def machine(self, src=SRC, label="label!", **kw):
        frames = []
        m = Machine(flowscript.compile(src), externs={"dim": lambda lv: lv * 10, "label": lambda: label},
                    observe=frames.append, **kw)
        return m, frames

    def test_a_run_as_observed(self):
        m, frames = self.machine()
        m.start()
        m.send("press", 2, "ann")
        m.send("press", 1, "bo")
        m.send("press", 1, "cy")
        self.assertTrue(all(is_observation(f) for f in frames))
        seen = []
        mon = Monitor(flowscript.compile(SRC),
                      on_state=lambda s: seen.append(("state", s)),
                      on_globals=lambda c: seen.append(("globals", c)),
                      on_handler=lambda st, ev, a: seen.append(("handler", st, ev, a)),
                      on_log=lambda lv, msg, *_: seen.append(("log", lv, msg)),
                      on_event=lambda n, a: seen.append(("event", n, a)),
                      on_call=lambda n, a, r: seen.append(("call", n, a, r)))
        mon.feed(b"".join(frames))
        self.assertEqual((mon.state, mon.globals), (m.state, m.globals))
        self.assertIn(("handler", "off", "press", [2, "ann"]), seen)
        self.assertIn(("log", "info", "press 2 by ann"), seen)
        self.assertIn(("call", "dim", [5], 50), seen)
        self.assertIn(("call", "label", [], "label!"), seen)
        self.assertIn(("event", "wobble", [1.5]), seen)
        self.assertIn(("globals", {"lit.level": 50}), seen)
        # the order things happened in: the entry into 'lit' and what its 'enter' did
        i = seen.index(("state", "lit"))
        self.assertEqual(seen[i + 1][:3], ("handler", "lit", "enter"))
        self.assertEqual([s[1] for s in seen if s[0] == "state"], ["off", "lit", "off"])

    def test_only_changes_are_sent(self):
        m, frames = self.machine()
        m.start()
        m.send("wobble", 0.0)                   # wob is 0.0 already: nothing to send
        n = len(frames)
        self.assertNotIn("globals", tags(frames[-2:]))
        m.send("wobble", -0.0)                  # == 0.0, but another value: sent
        self.assertIn("globals", tags(frames[n:]))
        n = len(frames)
        m.send("wobble", -0.0)
        self.assertNotIn("globals", tags(frames[n:]))
        # a handler that writes a variable many times sends it once, at its end
        m2, frames2 = self.machine(SRC.replace("    wob = x\n\nstate lit:",
                                               "    wob = x\n    wob = x + 1\n    wob = x + 2\n\nstate lit:"))
        m2.start()
        k = len(frames2)
        m2.send("wobble", 1.0)
        self.assertEqual(tags(frames2[k:]), ["handler", "globals"])

    def test_late_viewer_joins_at_a_keyframe(self):
        m, frames = self.machine()
        m.start()
        m.send("press", 2, "ann")
        cut = len(frames)
        m.record_keyframe()
        m.send("press", 1, "bo")
        mon = Monitor(flowscript.compile(SRC))
        mon.feed(b"".join(frames[1:cut]))
        self.assertFalse(mon.joined)
        mon.feed(b"".join(frames[cut:]))
        self.assertEqual((mon.state, mon.globals), (m.state, m.globals))
        m.send("press", 1, "x")                 # in 'lit' now: a handler is not running
        with self.assertRaises(RuntimeError):   # ... but a keyframe needs an idle machine
            m.vm.post(0 + m.program.event_index("press"), 1, "y")
            m.record_keyframe()

    def test_names_come_from_any_build_of_the_declarations(self):
        m, frames = self.machine()
        m.start()
        m.send("press", 3, "ann")
        # other code, comments and constants: the same variables in the same order
        other = SRC.replace("presses = presses + n", "presses = n + presses  # reordered") \
                   .replace("const MAX = 3", "const MAX = 3 + 0")
        mon = Monitor(flowscript.compile(other))
        mon.feed(b"".join(frames))
        self.assertIsNone(mon.mismatch)
        self.assertEqual(mon.globals, m.globals)
        # another variable: the stream no longer fits this source; the values still come
        mon = Monitor(flowscript.compile(SRC.replace('str last = ""', 'str last = ""\nint extra = 0')))
        mon.feed(b"".join(frames))
        self.assertIn("variables", mon.mismatch)
        self.assertEqual(mon.names[0], "global[0]")
        self.assertEqual(mon.state, m.state)
        # and no program at all
        mon = Monitor()
        mon.feed(b"".join(frames))
        self.assertEqual(mon["global[0]"], m["presses"])

    def test_refused_result_and_faults(self):
        m, frames = self.machine(label="x" * 100, str_len=16)        # too long for a string slot
        faults = []
        m.start()
        m.send("press", 3, "ann")
        m.send("press", 1, "bo")
        seen = []
        mon = Monitor(flowscript.compile(SRC), on_call=lambda n, a, r: seen.append((n, r)),
                      on_fault=lambda *f: faults.append(f))
        mon.feed(b"".join(frames))
        refused = [r for n, r in seen if n == "label"][0]
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.code, 12)
        self.assertEqual(refused.value.b, b"x" * 100)
        self.assertEqual(faults[0][:3], (12, "lit", "press"))
        self.assertEqual(mon.state, "lit")       # the handler was aborted before goto_state

    def test_damaged_frames(self):
        m, frames = self.machine()
        m.start()
        mon = Monitor()
        with self.assertRaises(MonitorError):
            mon.feed(bytes([3, 16, 9, 0]))       # a header of an unknown stream version
        mon = Monitor()
        mon.feed(frames[0])
        with self.assertRaises(MonitorError):
            mon.feed(bytes([2, 19, 1]))          # GLOBALS cut short

    def test_declaration_order_numbering(self):
        """SPEC 10.5: top-level variables in source order, then each state's, states in
        source order (not the order of the state table, where the initial state is first)."""
        src = ("int a = 0\nconst K = 1\nint b = 0\n"
               "state s1:\n  int x = 0\n  on enter:\n    pass\n"
               "initial state s2:\n  int y = 0\n  int z = 0\n  on enter:\n    pass\n"
               "float c = 0.0\n")
        p = flowscript.compile(src)
        self.assertEqual(list(p.global_names), ["a", "b", "c", "s1.x", "s2.y", "s2.z"])
        self.assertEqual(list(p.states), ["s2", "s1"])

    def test_view_session_in_monitor_mode(self):
        from flowscript.transport import Decoder, encode_frame
        from flowscript.view import Session
        m, frames = self.machine()
        m.start()
        m.send("press", 3, "ann")
        m.send("press", 1, "bo")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lamp.fs")
            with open(path, "w") as f:
                f.write(SRC.replace("press {n} by {who}", "pressed {n} times by {who}"))  # other code
            s = Session(path, live=False)
            dec = Decoder()
            s.feed(dec.feed(b"".join(encode_frame(f) for f in frames)) + dec.close())
            data = s.data(0, 0, 0, False)
        meta = data["meta"]
        self.assertEqual((meta["mode"], meta["joined"], meta["notes"]), ("monitor", True, []))
        self.assertEqual([g["name"] for g in meta["globals"]], ["presses", "last", "wob", "lit.level"])
        self.assertEqual(data["points"][-1][2:], [m.state, ["0", '"label!"', "1.5", "50"]])
        kinds = {e[2] for e in data["events"]}
        self.assertTrue({"handler", "log", "call", "event"} <= kinds, kinds)
        # the log text is the device's: its template came in the stream
        self.assertIn("info press 3 by ann", [e[3] for e in data["events"]])


if __name__ == "__main__":
    unittest.main()
