"""Snapshots, recordings and replay (SPEC 10) through the host API."""
import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flowscript
from flowscript import Machine, Replay
from flowscript.record import read_frames, ReplayError, TAG_NAMES

UTC = dt.timezone.utc
SRC = """
extern sensor() -> float
extern say(str text)

event go(int n, str who)
event halt
timer beat

const LIMIT = 4.0

float total = 0
int count = 0
str last = "none"
time since

initial state idle:
  on boot:
    since = now()

  on go(n, who):
    last = who + "/" + str(n)
    goto_state busy

state busy:
  int beats = 0

  on enter:
    start beat every 100

  on beat:
    beats = beats + 1
    count = count + 1
    total = total + sensor()
    say(last)
    if total > LIMIT:
      warning "total {total} over {LIMIT} after {beats} beats"

  on halt:
    goto_state idle
"""


class Host:
    """A deterministic host: a sensor that ramps, a clock that moves."""

    def __init__(self):
        self.t = dt.datetime(2026, 11, 2, 9, tzinfo=UTC)
        self.v = 0.5
        self.said = []

    def sensor(self):
        self.v += 0.25
        return self.v

    def machine(self, program, **kw):
        return Machine(program, externs={"sensor": self.sensor, "say": self.said.append},
                       clock=lambda: self.t, **kw)


def drive(m, host):
    m.start()
    m.send("go", 3, "ann")
    for _ in range(5):
        host.t += dt.timedelta(milliseconds=100)
        m.advance(100)
    m.send("halt")
    m.send("go", 7, "bo")
    m.advance(250)


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.p = flowscript.compile(SRC)

    def test_restore_continues_exactly(self):
        h1 = Host()
        a = h1.machine(self.p).start()
        a.send("go", 3, "ann")
        a.advance(250)
        snap = a.snapshot()
        h2 = Host()
        h2.v = h1.v
        b = h2.machine(self.p)
        b.restore(snap)
        self.assertEqual((b.state, b.globals), (a.state, a.globals))
        self.assertEqual(b["busy.beats"], 2)
        self.assertIsInstance(b["since"], dt.datetime)
        for m in (a, b):                          # the same future
            m.advance(300)
            m.send("halt")
        self.assertEqual(a.globals, b.globals)
        self.assertEqual(h1.said, ["ann/3"] * 5)
        self.assertEqual(h2.said, ["ann/3"] * 3)  # b only saw what came after the snapshot
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_errors(self):
        m = Host().machine(self.p).start()
        m.post("go", 1, "x")
        with self.assertRaisesRegex(RuntimeError, "busy"):
            m.snapshot()
        m.run()
        snap = m.snapshot()
        other = Host().machine(flowscript.compile(SRC.replace("LIMIT = 4.0", "LIMIT = 5.0")))
        with self.assertRaisesRegex(ValueError, "another image"):
            other.restore(snap)
        with self.assertRaisesRegex(ValueError, "truncated"):
            Host().machine(self.p).restore(snap[:-3])
        with self.assertRaisesRegex(ValueError, "not a FlowScript snapshot"):
            Host().machine(self.p).restore(b"nope" + snap[4:])

    def test_snapshot_is_small(self):
        m = Host().machine(self.p).start()
        m.send("go", 3, "a much longer name")
        self.assertLess(len(m.snapshot()), 120)


class Recording(unittest.TestCase):
    def setUp(self):
        self.p = flowscript.compile(SRC)

    def record(self, keyframe_after=None):
        frames = []
        host = Host()
        m = host.machine(self.p, record=frames.append)
        drive(m, host)
        m.record_flush()
        return m, frames

    def test_replay_follows_everything(self):
        m, frames = self.record()
        logs, events, states = [], [], []
        viewer = Replay(self.p, on_log=lambda *a: logs.append(a[1]), on_state=states.append)
        viewer.feed(b"".join(frames))
        self.assertTrue(viewer.joined)
        self.assertEqual(viewer.state, m.state)
        self.assertEqual(viewer.globals, m.globals)
        self.assertEqual(viewer.snapshot(), m.snapshot())
        self.assertEqual(states, ["idle", "busy", "idle", "busy"])
        self.assertTrue(logs and logs[0].startswith("total 4.5 over 4.0"))
        tags = [TAG_NAMES[t] for t, _p in read_frames(b"".join(frames))]
        self.assertEqual(tags[0], "start")
        self.assertIn("result", tags)                # the sensor's values
        self.assertIn("clock", tags)                 # now() in boot
        self.assertLess(len(b"".join(frames)), 400)

    def test_any_chunking(self):
        m, frames = self.record()
        data = b"".join(frames)
        rng = random.Random(7)
        viewer = Replay(self.p)
        i = 0
        while i < len(data):
            n = rng.randint(1, 9)
            viewer.feed(data[i:i + n])
            i += n
        self.assertEqual(viewer.globals, m.globals)

    def test_joining_at_a_keyframe(self):
        frames = []
        host = Host()
        m = host.machine(self.p, record=frames.append)
        m.start()
        m.send("go", 3, "ann")
        m.advance(200)
        cut = len(frames)
        m.record_keyframe()
        m.advance(300)
        m.send("halt")
        m.record_flush()
        late = Replay(self.p)
        late.feed(b"".join(frames[1:cut]))        # joined late: no START, nothing to go on yet
        self.assertFalse(late.joined)
        late.feed(b"".join(frames[cut:]))
        self.assertTrue(late.joined)
        self.assertEqual((late.state, late.globals), (m.state, m.globals))

    def test_wrong_program_is_refused(self):
        _m, frames = self.record()
        other = flowscript.compile(SRC.replace("LIMIT = 4.0", "LIMIT = 5.0"))
        with self.assertRaisesRegex(ReplayError, "another image"):
            Replay(other).feed(b"".join(frames))

    def test_bytecode_viewer(self):
        """A viewer with only the .fsb (no source): state and logs, no variable names."""
        m, frames = self.record()
        viewer = Replay(self.p.to_bytes())
        viewer.feed(b"".join(frames))
        self.assertEqual(viewer.state, m.state)
        with self.assertRaisesRegex(KeyError, "no global names"):
            viewer["total"]
        # a device running the -g image: the viewer needs that image, or the source
        frames, host = [], Host()
        dev = host.machine(self.p.to_bytes(debug=True), record=frames.append)
        drive(dev, host)
        dev.record_flush()
        for program in (self.p.to_bytes(debug=True), self.p):
            v = Replay(program)
            v.feed(b"".join(frames))
            self.assertEqual(v["total"], m["total"])
        with self.assertRaisesRegex(ReplayError, "another image"):
            Replay(self.p.to_bytes()).feed(b"".join(frames))

    def test_source_viewer_follows_a_debug_device_at_keyframes(self):
        """Replay(program) follows the image a recording names: a -g device's keyframes restore."""
        for debug in (True, False):
            frames, host = [], Host()
            dev = host.machine(self.p.to_bytes(debug=debug), record=frames.append)
            dev.start()
            dev.send("go", 3, "ann")
            dev.advance(200)
            cut = len(frames)
            dev.record_keyframe()
            dev.advance(300)
            dev.send("halt")
            dev.record_flush()
            for chunk in (b"".join(frames), b"".join(frames[cut:])):    # from the start, and late
                v = Replay(self.p)
                v.feed(chunk)
                self.assertEqual(v.state, dev.state)
                self.assertEqual(v.snapshot(), dev.snapshot())       # every global and timer
                self.assertEqual(v["count"], 5)                       # names from the source

    def test_cli_replay_with_device_image(self):
        """flowscript replay FILE.fs REC --image DEVICE.fsb: a -g device, joined at a keyframe,
        with the state diagram drawn from the source."""
        import subprocess
        import tempfile
        frames, host = [], Host()
        dev = host.machine(self.p.to_bytes(debug=True), record=frames.append)
        dev.start()
        dev.send("go", 3, "ann")
        dev.advance(200)
        cut = len(frames)
        dev.record_keyframe()
        dev.advance(300)
        dev.record_flush()
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        env = dict(os.environ, PYTHONPATH=root)
        with tempfile.TemporaryDirectory() as d:
            src, fsb, rec, svg = (os.path.join(d, n) for n in ("p.fs", "p.fsb", "p.rec", "p.svg"))
            with open(src, "w") as f:
                f.write(SRC)
            with open(fsb, "wb") as f:
                f.write(self.p.to_bytes(debug=True))
            with open(rec, "wb") as f:
                f.write(b"".join(frames[cut:]))          # a viewer that joins late

            def replay(file, *extra):
                return subprocess.run([sys.executable, "-m", "flowscript", "replay", file, rec] + list(extra),
                                      capture_output=True, text=True, env=env)
            r = replay(src, "--svg", svg)                 # the source alone follows the -g image too
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("global[1] = %d" % dev["count"], r.stdout)
            r = replay(src, "--image", fsb, "--svg", svg)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stderr, "")
            self.assertIn("global[1] = %d" % dev["count"], r.stdout)
            with open(svg) as f:
                self.assertIn('data-state="busy"', f.read())
            # a source the image was not compiled from: still replays (the image decides),
            # with a warning that the state diagram may not match
            with open(src, "w") as f:
                f.write(SRC.replace("LIMIT = 4.0", "LIMIT = 5.0"))
            r = replay(src, "--image", fsb)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("not compiled from", r.stderr)
            # FILE is already bytecode: --image makes no sense
            r = replay(fsb, "--image", fsb)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--image", r.stderr)

if __name__ == "__main__":
    unittest.main()
