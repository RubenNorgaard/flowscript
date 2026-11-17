"""flowscript view: follow a device's recording live in the browser, or look through a saved one.

    stty -f /dev/cu.usbmodem1 raw -echo           # Linux: stty -F /dev/ttyACM0 raw -echo
    cat /dev/cu.usbmodem1 | flowscript view app.fs > run1.log
    flowscript view app.fs run1.log

The input is the device's output: console text with the recording on '#FS:' lines
(SPEC 10.4), or a binary recording.  The page shows the state diagram, every variable,
log statements, faults, raised events, extern calls and the other console lines, with a
timeline to go back to any point.  When stdout is not a terminal, the input is written
to it as it arrives (text; binary frames as '#FS:' lines), with '#T:' lines giving the
arrival time, so `> FILE` keeps a copy that `flowscript view` and `flowscript replay`
read back.  All the stdlib: no serial library, the shell opens the port.
"""

import json
import os
import stat
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import values as V
from ..api import Replay, compile as fs_compile, load as fs_load
from ..bytecode import Program
from ..record import ReplayError, image_hash, Reader, START, KEYFRAME
from ..transport import Decoder, encode_frame, encode_time
from ..observe import Monitor, Refused, is_observation

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TYPE_NAMES = {V.INT: "int", V.FLOAT: "float", V.STR: "str", V.TIME: "time"}
MAX_POINTS = 50000          # kept in memory; older ones are dropped while following live
MAX_EVENTS = 20000


def now_ms():
    return int(time.time() * 1000)


class Session:
    """The program, what the device sent and everything seen so far.  Thread safe.

    A device streams a recording (its inputs: the run is replayed here, which needs its
    exact image) or observations (monitor mode, SPEC 10.5: shown as they are, the source
    only names them); each frame goes to the one it belongs to."""

    def __init__(self, program_path, image_path=None, input_label="stdin", live=True):
        self.source = None
        if program_path.endswith(".fsb"):
            with open(program_path, "rb") as f:
                raw = f.read()
            self.program = fs_load(raw)
            self.target = raw                       # replay these exact bytes
        else:
            with open(program_path, encoding="utf-8") as f:
                self.source = f.read()
            self.program = fs_compile(self.source, filename=program_path)
            self.target = self.program              # follows the stripped or the -g image
        self.notes = []
        if image_path:
            with open(image_path, "rb") as f:
                raw = f.read()
            image_prog = Program.from_bytes(raw)
            if self.source is not None and raw not in (self.program.to_bytes(debug=False),
                                                       self.program.to_bytes(debug=True)):
                self.notes.append("%s was not compiled from %s: the state diagram may not match"
                                  % (os.path.basename(image_path), os.path.basename(program_path)))
            if not self.program.global_names:
                self.program = image_prog
            self.target = raw
        self.name = getattr(self.program, "name", None) or os.path.splitext(os.path.basename(program_path))[0]
        self.file = os.path.basename(program_path)
        self.input_label = input_label
        self.live = live
        self.eof = False
        names = list(self.program.global_names or [])
        self.global_names = [names[i] if i < len(names) and names[i] else "global[%d]" % i
                             for i in range(len(self.program.globals))]
        self.global_types = [TYPE_NAMES.get(V.type_of(v), "?") for v in self.program.globals]
        self.hashes = self._image_hashes()

        self.cond = threading.Condition()
        self.version = 0
        self.points, self.pbase = [], 0             # [t, frame, state, [value texts]]
        self.events, self.ebase = [], 0             # [point, t, kind, text]
        self.frames = self.bad = 0
        self.t = None                               # time of what is being read (ms) or None
        self.own_clock = live                       # until the input brings its own #T lines
        self._last = None
        self._pending_events = False
        self.mode = None                            # "replay" or "monitor", from the frames seen
        self._new_replay()
        self._new_monitor()

    def _image_hashes(self):
        if isinstance(self.target, bytes):
            return {"image": "%08x" % image_hash(self.target)}
        return {"stripped": "%08x" % image_hash(self.program.to_bytes(debug=False)),
                "-g": "%08x" % image_hash(self.program.to_bytes(debug=True))}

    def _new_replay(self):
        self.replay = Replay(self.target, on_log=self._on_log, on_event=self._on_event,
                             on_fault=self._on_fault, on_extern=self._on_extern)

    def _new_monitor(self):
        def show_args(args):
            return ", ".join(V.show(a) for a in args)

        def handler(st, ev, args):
            self._event("handler", "%s%s(%s)" % (st + " · " if st else "", ev, show_args(args)))

        def call(name, args, result):
            c = "%s(%s)" % (name, show_args(args))
            if isinstance(result, Refused):
                c += " -> refused (fault %d)" % result.code
            elif result is not None:
                c += " -> " + V.show(result)
            self._event("call", c)

        def fault(code, st, ev, pc):
            from ..bytecode import FAULTS
            where = " in %s.on %s" % (st, ev) if ev else ""
            self._event("fault", "fault %d %s%s (pc %d)" % (code, FAULTS.get(code, ""), where, pc))

        def start(fresh):
            if fresh:
                m = self.monitor
                self._event("handler", "program %s started" % (m.name or "?"))

        self.monitor = Monitor(self.program, raw=True, on_handler=handler, on_call=call, on_fault=fault,
                               on_start=start, on_log=lambda lv, msg, *_: self._event("log", "%s %s" % (lv, msg)),
                               on_event=lambda n, a: self._event("event", "%s(%s)" % (n, show_args(a))))

    # ---- replay callbacks (lock held) --------------------------------------------------
    def _event(self, kind, text):
        self.events.append([self.pbase + len(self.points), self.t, kind, text])
        self._pending_events = True
        if len(self.events) > MAX_EVENTS:
            drop = len(self.events) - MAX_EVENTS
            del self.events[:drop]
            self.ebase += drop

    def _on_log(self, level, message, *_):
        self._event("log", "%s %s" % (level, message))

    def _on_event(self, name, args):
        self._event("event", "%s(%s)" % (name, ", ".join(_show_py(a) for a in args)))

    def _on_fault(self, fault):
        self._event("fault", str(fault))

    def _on_extern(self, name, args, result):
        call = "%s(%s)" % (name, ", ".join(_show_py(a) for a in args))
        self._event("call", call if result is None else "%s -> %s" % (call, _show_py(result)))

    # ---- input ---------------------------------------------------------------------------
    def feed(self, items):
        """Decoded input items (transport.Decoder) -> replay, timeline, events."""
        with self.cond:
            for kind, value in items:
                if kind == "time":
                    self.own_clock = False
                    self.t = value
                elif kind == "text":
                    self._event("text", value)
                elif kind == "bad":
                    self.bad += 1
                    self._event("error", "damaged frame line dropped: %s" % value)
                elif kind == "frame":
                    self._frame(value)
            self._settle()
            self.version += 1
            self.cond.notify_all()

    def tick_clock(self):
        if self.own_clock:
            self.t = now_ms()

    def _frame(self, frame):
        self.frames += 1
        mode = "monitor" if is_observation(frame) else "replay"
        if mode != self.mode:
            if self.mode is not None:
                self._event("error", "the device switched to %s mode" % mode)
            self.mode = mode
            self._last = None
        if mode == "monitor":
            try:
                self.monitor.feed(frame)
            except ValueError as e:                 # MonitorError: a damaged / unknown frame
                self._event("error", "%s: waiting for the next keyframe" % e)
                self._new_monitor()
                self._last = None
            self._settle()
            return
        try:
            self.replay.feed(frame)
        except ReplayError as e:
            self._event("error", self._explain(frame, e))
            self._new_replay()
            self._last = None
        except Exception as e:                      # a bad frame must not stop the viewer
            self._event("error", "replay failed (%s: %s): waiting for the next keyframe"
                        % (type(e).__name__, e))
            self._new_replay()
            self._last = None
        self._settle()

    def _explain(self, frame, e):
        r = Reader(frame)
        try:
            r.varint()
            tag = r.byte()
            h = r.unpack("I") if tag in (START, KEYFRAME) else None
        except ValueError:
            h = None
        if h is not None and "another image" in str(e):
            have = ", ".join("%s %s" % (v, k) for k, v in self.hashes.items())
            return ("the device runs image %08x, %s is image %s: compile the device's program "
                    "from this source, or pass the .fsb it runs with --image" % (h, self.file, have))
        return "%s: waiting for the next keyframe" % e

    def _settle(self):
        """Add a point to the timeline if the state or a variable changed, or events came."""
        if self.mode == "monitor":
            if not self.monitor.joined:
                return
            cur = (self.monitor.state, [V.show(v) for v in self.monitor.raw])
        else:
            if not self.replay.joined:
                return
            vm = self.replay._rp.vm
            cur = (vm.state_name(), [V.show(v) for v in vm.globals])
        if cur == self._last and not self._pending_events:
            return
        self._last = cur
        self._pending_events = False
        self.points.append([self.t, self.frames, cur[0], cur[1]])
        if len(self.points) > MAX_POINTS:
            drop = len(self.points) - MAX_POINTS
            del self.points[:drop]
            self.pbase += drop

    def end(self):
        with self.cond:
            self.eof = True
            self.version += 1
            self.cond.notify_all()

    # ---- output ----------------------------------------------------------------------------
    def data(self, p, e, v, wait):
        with self.cond:
            if wait and self.version <= v:
                self.cond.wait(timeout=20)
            p, e = max(p, self.pbase), max(e, self.ebase)
            m = self.monitor
            if self.mode == "monitor" and m.joined:
                gl = [{"name": n, "type": TYPE_NAMES.get(t, "?")} for n, t in zip(m.names, m.types)]
                joined = True
                device = "%s v%s · image %08x" % (m.name or "?", m.version, m.image_hash)
                notes = self.notes + ([m.mismatch] if m.mismatch else [])
            else:
                gl = [{"name": n, "type": t} for n, t in zip(self.global_names, self.global_types)]
                joined = self.mode == "replay" and self.replay.joined
                device = None
                notes = self.notes
            return {
                "version": self.version,
                "meta": {
                    "name": self.name, "file": self.file, "input": self.input_label,
                    "live": self.live, "eof": self.eof, "joined": joined, "mode": self.mode,
                    "device": device, "frames": self.frames, "bad": self.bad,
                    "has_source": self.source is not None, "globals": gl,
                    "states": list(self.program.states), "hashes": self.hashes, "notes": notes,
                },
                "pbase": p, "points": self.points[p - self.pbase:],
                "ebase": e, "events": self.events[e - self.ebase:],
            }

    _svg = {}

    def svg(self, view, state):
        from ..svg import to_svg
        if self.source is None:
            return None
        key = (view, state)
        if key not in self._svg:
            self._svg[key] = to_svg(self.program, view=view, state=state or None, width="100%")
        return self._svg[key]


def _show_py(v):
    if isinstance(v, str):
        return V.show(V.Str(v))
    if hasattr(v, "isoformat"):
        return v.isoformat().replace("+00:00", "Z")
    return V.show(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else repr(v)


# ---- input thread --------------------------------------------------------------------------
class Echo:
    """Write the input to stdout as text, with #T: arrival times (only when stdout is
    redirected).  Stops quietly if the reader goes away."""

    def __init__(self, out, stamp):
        self.out, self.stamp, self.t_written = out, stamp, None

    def time(self, t):
        if self.out and self.stamp and t is not None and t != self.t_written:
            self.write(encode_time(t))
            self.t_written = t

    def write(self, data):
        if not self.out:
            return
        try:
            self.out.write(data)
        except (BrokenPipeError, OSError, ValueError):
            self.out = None

    def flush(self):
        if self.out:
            try:
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.out = None


def read_input(session, fd, echo):
    pending_lines = []
    dec = Decoder(lines=pending_lines.append)
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError as e:
            session.feed([("text", "[view] input error: %s" % e)])
            chunk = b""
        if not chunk:
            items = dec.close()
        else:
            session.tick_clock()
            items = dec.feed(chunk)
        if any(k == "time" for k, _v in items):
            echo.stamp = False                      # the input has its own times: keep those
        elif pending_lines or (dec.mode == "binary" and items):
            echo.time(session.t)
        for line in pending_lines:
            echo.write(line + b"\n")
        pending_lines.clear()
        if dec.mode == "binary":
            for kind, value in items:
                if kind == "frame":
                    echo.write(encode_frame(value))
        echo.flush()
        if items:
            session.feed(items)
        if not chunk:
            session.end()
            return


# ---- HTTP -----------------------------------------------------------------------------------
def make_handler(session):
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        page = f.read()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            q = {}
            for part in qs.split("&"):
                k, _, val = part.partition("=")
                if k:
                    from urllib.parse import unquote
                    q[k] = unquote(val)
            if path == "/":
                self._send(page, "text/html; charset=utf-8")
            elif path == "/data":
                def num(k):
                    try:
                        return int(q.get(k, 0))
                    except ValueError:
                        return 0
                d = session.data(num("p"), num("e"), num("v"), q.get("wait") == "1")
                self._send(json.dumps(d), "application/json")
            elif path == "/svg":
                view = q.get("view") if q.get("view") in ("states", "flow") else "states"
                svg = session.svg(view, q.get("state"))
                if svg is None:
                    self._send("no source: bytecode has no flowchart", "text/plain", 404)
                else:
                    self._send(svg, "image/svg+xml; charset=utf-8")
            elif path == "/source":
                self._send(session.source or "", "text/plain; charset=utf-8")
            else:
                self._send("not found", "text/plain", 404)

    return Handler


def serve(program_path, input_path="-", image=None, port=8643, open_browser=True, echo=None):
    """Run the viewer until Ctrl+C.  echo: write the input to stdout (default: when stdout
    is not a terminal)."""
    err = lambda msg: print(msg, file=sys.stderr, flush=True)
    if input_path == "-":
        if sys.stdin.isatty():
            raise SystemExit("flowscript view: pipe the device's output in, e.g.\n"
                             "  cat /dev/cu.usbmodem1 | flowscript view %s > run.log\n"
                             "or give a saved file: flowscript view %s run.log" % (program_path, program_path))
        fd, label = sys.stdin.fileno(), "stdin"
    else:
        fd, label = os.open(input_path, os.O_RDONLY), input_path
    live = not stat.S_ISREG(os.fstat(fd).st_mode)
    if input_path == "-" and not live:
        label = "stdin (a file)"
    session = Session(program_path, image, label, live)
    for n in session.notes:
        err("flowscript view: warning: %s" % n)
    if echo is None:
        echo = not sys.stdout.isatty()
    out = Echo(sys.stdout.buffer if echo else None, stamp=live)
    threading.Thread(target=read_input, args=(session, fd, out), daemon=True).start()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session))
    except OSError as e:
        import errno
        if e.errno == errno.EADDRINUSE:
            raise SystemExit("flowscript view: port %d is already in use (another viewer?): "
                             "pick another with --port N" % port)
        raise
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    err("flowscript view: %s on %s  (Ctrl+C to stop)" % (session.name, url))
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        out.flush()
