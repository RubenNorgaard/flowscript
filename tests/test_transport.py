"""The text transport for recordings (SPEC 10.4): #FS: frame lines, #T: times, console text."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import flowscript
from flowscript.transport import Decoder, encode_frame, encode_time, looks_binary, recording_bytes

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = """
event go(int n, str who)
int count = 0
str last = ""

initial state idle:
  on go(n, who):
    count = count + n
    last = who
    if count > 5:
      goto_state done

state done:
  on go(n, who):
    count = 0
    goto_state idle
"""


def record(keyframe_at=None):
    p = flowscript.compile(SRC)
    frames = []
    m = flowscript.Machine(p, record=frames.append)
    m.start()
    for i, who in enumerate(["ann", "bo", "cy", "di"]):
        if i == keyframe_at:
            m.record_keyframe()
        m.send("go", 2, who)
    m.record_flush()
    return p, m, frames


def text_stream(frames):
    """What a device writes: boot noise, frames as lines, a log line run into a frame."""
    out = b"\r\nESP-ROM:esp32c6-20220919\r\nboot: hello\r\n"
    for i, f in enumerate(frames):
        out += (b"I (42) app: tick " if i == 2 else b"") + encode_frame(f).replace(b"\n", b"\r\n")
        out += b"[script] something\r\n"
    return out


def decode(data, chunk):
    dec = Decoder()
    items = []
    for i in range(0, len(data), chunk):
        items += dec.feed(data[i:i + chunk])
    return items + dec.close()


class Transport(unittest.TestCase):
    def test_text_any_chunking(self):
        _p, _m, frames = record()
        data = text_stream(frames)
        for chunk in (1, 2, 3, 7, 64, len(data)):
            items = decode(data, chunk)
            self.assertEqual([v for k, v in items if k == "frame"], frames, chunk)
            text = [v for k, v in items if k == "text"]
            self.assertEqual(text[:2], ["ESP-ROM:esp32c6-20220919", "boot: hello"])
            self.assertIn("I (42) app: tick", text)          # the text before a frame on its line
            self.assertNotIn("bad", [k for k, _v in items])

    def test_binary_detected_from_any_first_frame(self):
        _p, _m, frames = record(keyframe_at=2)
        data = b"".join(frames)
        cut = next(i for i, f in enumerate(frames) if f[1:2] == b"\x02")    # the keyframe
        for start in (0, cut - 1, cut, cut + 1):                            # START, STEPS, KEYFRAME, POST
            for chunk in (1, 5, 1000):
                items = decode(b"".join(frames[start:]), chunk)
                self.assertEqual([v for k, v in items], frames[start:], (start, chunk))
        self.assertEqual(decode(frames[0], 1), [("frame", frames[0])])      # a single frame
        self.assertIs(looks_binary(b"\r\nESP-ROM:esp32c6"), False)
        self.assertIs(looks_binary(b"#FS:07"), False)
        self.assertIsNone(looks_binary(frames[0][:3]))

    def test_times_and_damaged_lines(self):
        _p, _m, frames = record()
        good = encode_frame(frames[1])
        data = (encode_time(1794433584097) + good + b"#FS:0701e3c8\n"   # length says 7, has 3
                + b"#FS:zz\n" + b"#T:junk\n" + good.rstrip(b"\n"))      # no final newline
        items = decode(data, 3)
        self.assertEqual([k for k, _v in items], ["time", "frame", "bad", "bad", "text", "frame"])
        self.assertEqual(items[0][1], 1794433584097)

    def test_echo_sees_every_line(self):
        _p, _m, frames = record()
        data = text_stream(frames)
        seen = []
        dec = Decoder(lines=seen.append)
        dec.feed(data)
        dec.close()
        self.assertEqual(b"\n".join(seen) + b"\n", data)

    def test_replay_cli_reads_text(self):
        _p, m, frames = record()
        data = text_stream(frames)
        self.assertEqual(b"".join(recording_bytes([data[:10], data[10:]])), b"".join(frames))
        path = os.path.join(ROOT, "tests", "_transport.fs")
        with open(path, "w") as f:
            f.write(SRC)
        try:
            env = dict(os.environ, PYTHONPATH=ROOT)
            r = subprocess.run([sys.executable, "-m", "flowscript", "replay", path, "-"], input=data,
                               capture_output=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"state done", r.stdout)
            self.assertIn(b'global[1] = "cy"', r.stdout)     # "di" came in done: last kept
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
