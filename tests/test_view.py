"""flowscript view: the session (timeline, events, joining, wrong images) and the command
end to end (stdin as a pipe or a file, the echo with #T: times, the HTTP data)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import flowscript
from flowscript.transport import Decoder, encode_frame, encode_time
from flowscript.view import Session

SRC = """flow counter version 1

extern led(int lit)
event go(int n, str who)
int count = 0
str last = ""

initial state idle:
  on go(n, who):
    count = count + n
    last = who
    info "go {n} from {who}"
    if count > 5:
      goto_state busy

state busy:
  on enter:
    led(1)

  on go(n, who):
    count = 0
    goto_state idle

  on exit:
    led(0)
"""


def device_text(source=SRC, debug=True, keyframe_at=None, n=6):
    """A device's console: boot text, then #FS: lines, as the C VM's sink would write them."""
    p = flowscript.compile(source)
    frames = []
    m = flowscript.Machine(p.to_bytes(debug=debug), externs={"led": lambda x: None}, record=frames.append)
    m.start()
    for i in range(n):
        if i == keyframe_at:
            m.record_keyframe()
        m.send("go", 2, "w%d" % i)
        m.record_flush()
    out = b"ESP-ROM:boot\r\n" + b"".join(encode_frame(f) for f in frames)
    return out, frames, m


def session_with(data, path, **kw):
    s = Session(path, live=False, **kw)
    dec = Decoder()
    s.feed(dec.feed(data) + dec.close())
    return s


def free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class ViewSession(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.fs = os.path.join(self.dir.name, "counter.fs")
        with open(self.fs, "w") as f:
            f.write(SRC)

    def tearDown(self):
        self.dir.cleanup()

    def test_timeline_and_events(self):
        data, _frames, m = device_text()
        s = session_with(data, self.fs)
        d = s.data(0, 0, 0, False)
        self.assertTrue(d["meta"]["joined"])
        self.assertEqual([g["name"] for g in d["meta"]["globals"]], ["count", "last"])
        last = d["points"][-1]
        self.assertEqual((last[2], last[3]), (m.state, [str(m["count"]), '"%s"' % m["last"]]))
        states = [p[2] for p in d["points"]]
        self.assertIn("busy", states)
        kinds = {e[2] for e in d["events"]}
        self.assertTrue({"text", "log", "call"} <= kinds, kinds)
        # every event points at a point that exists
        self.assertTrue(all(e[0] < len(d["points"]) for e in d["events"]))

    def test_late_join_of_a_debug_device(self):
        data, frames, m = device_text(keyframe_at=3)
        cut = next(i for i, f in enumerate(frames) if f[1] == 2)
        late = b"".join(encode_frame(f) for f in frames[cut - 1:])         # starts with a STEPS frame
        s = session_with(late, self.fs)
        d = s.data(0, 0, 0, False)
        self.assertTrue(d["meta"]["joined"])
        self.assertEqual(d["points"][-1][3][0], str(m["count"]))
        self.assertNotIn("error", {e[2] for e in d["events"]})

    def test_another_program_is_explained(self):
        data, _f, _m = device_text(SRC.replace("count > 5", "count > 7"))
        s = session_with(data, self.fs)
        d = s.data(0, 0, 0, False)
        self.assertFalse(d["meta"]["joined"])
        errors = [e[3] for e in d["events"] if e[2] == "error"]
        self.assertTrue(errors and "the device runs image" in errors[0] and "--image" in errors[0], errors)

    def test_image_option_and_bytecode_only(self):
        other = SRC.replace("count > 5", "count > 7")
        data, _f, m = device_text(other)
        fsb = os.path.join(self.dir.name, "device.fsb")
        with open(fsb, "wb") as f:
            f.write(flowscript.compile(other).to_bytes(debug=True))
        s = session_with(data, self.fs, image_path=fsb)                      # source draws, image runs
        self.assertTrue(s.notes and "not compiled from" in s.notes[0])
        self.assertEqual(s.data(0, 0, 0, False)["points"][-1][3][0], str(m["count"]))
        s = session_with(data, fsb)                                          # bytecode alone: no diagram
        self.assertTrue(s.data(0, 0, 0, False)["meta"]["joined"])
        self.assertIsNone(s.svg("states", "idle"))

    def test_damaged_line_is_counted_and_skipped(self):
        data, _f, m = device_text()
        lines = data.split(b"\n")
        lines.insert(3, lines[3][:9])                                        # a frame line cut short
        s = session_with(b"\n".join(lines), self.fs)
        d = s.data(0, 0, 0, False)
        self.assertEqual(d["meta"]["bad"], 1)
        self.assertEqual(d["points"][-1][3][0], str(m["count"]))


class ViewCommand(unittest.TestCase):
    """The command itself: input from a pipe (live: #T: times added) or a file."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.fs = os.path.join(self.dir.name, "counter.fs")
        with open(self.fs, "w") as f:
            f.write(SRC)

    def tearDown(self):
        self.dir.cleanup()

    def run_view(self, data, as_file):
        port = free_port()
        args = [sys.executable, "-m", "flowscript", "view", self.fs, "--no-browser", "--port", str(port)]
        stdin = subprocess.PIPE
        if as_file:
            path = os.path.join(self.dir.name, "in.log")
            with open(path, "wb") as f:
                f.write(data)
            args.insert(5, path)
            stdin = subprocess.DEVNULL
        proc = subprocess.Popen(args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(os.environ, PYTHONPATH=ROOT))
        try:
            if not as_file:
                proc.stdin.write(data[:40])                                  # in two pieces, like a port
                proc.stdin.flush()
                time.sleep(0.05)
                proc.stdin.write(data[40:])
                proc.stdin.close()
                proc.stdin = None                                            # communicate() must not touch it
            d = None
            for _ in range(100):
                try:
                    d = json.load(urllib.request.urlopen("http://127.0.0.1:%d/data?p=0&e=0" % port, timeout=2))
                    if d["meta"]["eof"]:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            page = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read()
            svg = urllib.request.urlopen("http://127.0.0.1:%d/svg?view=states&state=busy" % port, timeout=2).read()
        finally:
            proc.terminate()
            out, err = proc.communicate(timeout=10)
        return d, out, err, page, svg

    def test_pipe(self):
        data, _frames, m = device_text()
        d, out, err, page, svg = self.run_view(data, as_file=False)
        self.assertTrue(d["meta"]["live"] and d["meta"]["eof"] and d["meta"]["joined"], err)
        self.assertEqual(d["points"][-1][3][0], str(m["count"]))
        self.assertIn(b"<title>FlowScript View</title>", page)
        self.assertIn(b'data-state="busy"', svg)
        self.assertIn(b"http://127.0.0.1:", err)                             # messages go to stderr
        # stdout: the input, line for line, with #T: arrival times added
        self.assertTrue(out.startswith(b"#T:"), out[:40])
        self.assertEqual(b"".join(l + b"\n" for l in out.split(b"\n")[:-1] if not l.startswith(b"#T:")), data)
        # ... which reads back as the same run
        again = session_with(out, self.fs)
        self.assertEqual(again.data(0, 0, 0, False)["points"][-1][3], d["points"][-1][3])
        self.assertTrue(all(p[0] for p in again.points))                    # every point has a time

    def test_file(self):
        data, frames, m = device_text()
        stamped = encode_time(1794433584000) + data
        d, out, err, _page, _svg = self.run_view(stamped, as_file=True)
        self.assertFalse(d["meta"]["live"])
        self.assertEqual(d["points"][-1][0], 1794433584000)                 # its own time, none added
        self.assertEqual(out, stamped)
        # a binary recording in: '#FS:' lines out
        binary = b"".join(frames)
        d, out, err, _page, _svg = self.run_view(binary, as_file=True)
        self.assertTrue(d["meta"]["joined"], err)
        self.assertEqual(out, b"".join(encode_frame(f) for f in frames))


if __name__ == "__main__":
    unittest.main()
