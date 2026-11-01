"""FlowScript 3-view debugger: a tiny stdlib HTTP server around the compiler + Python VM.

    python3 -m flowscript debug examples/gps_tracker.fs

Views (in the browser): source | bytecode | flowchart, all following the execution.
"""

import json
import os
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

from ..lexer import FlowError
from ..parser import parse, parse_expression
from ..sizes import size_report
from ..printer import to_source, expr_to_str
from ..compiler import compile_ast
from ..bytecode import disassemble, FAULTS
from ..vm import VM, IDLE, DISPATCHED, RUNNING, DONE, FAULT
from .. import values as V


def jv(v):
    """A VM value for JSON: an int as a number, anything else as its canonical text
    (floats can be nan/inf, which JSON can't carry)."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    try:
        return V.show(v)
    except TypeError:
        return str(v)

STATIC = os.path.join(os.path.dirname(__file__), "static")
LOG_LIMIT = 400

DEFAULT_SOURCE = """flow hello

extern led(on)
event button
timer blink

var count = 0

on boot
  start blink every 500ms
end

on blink
  count = count + 1
  led(count % 2)
end

on button
  if count > 10 and not count > 100
    stop blink
  end
end
"""


class Session:
    def __init__(self, path=None):
        self.path = path
        if path and os.path.exists(path):
            with open(path) as f:
                self.source = f.read()
        else:
            self.source = DEFAULT_SOURCE
        self.extern_values = {}
        self.breakpoints = set()     # source lines
        self.ast = None
        self.prog = None
        self.dbg = None
        self.vm = None
        self.errors = []
        self.compile(self.source)

    # -- compilation -------------------------------------------------------------------
    def compile(self, source):
        self.source = source
        self.errors = []
        self.prog = None
        self.vm = None
        try:
            self.ast = parse(source)
        except FlowError as e:
            self.errors = [e.to_dict()]
            return
        try:
            self.prog, self.dbg = compile_ast(self.ast)
        except FlowError as e:
            self.errors = [e.to_dict()]
            return
        # per-statement expression lists, used to clear stale gate values
        self.stmt_exprs = {}
        for nid, sid in self.dbg.stmt_of.items():
            if nid != sid:
                self.stmt_exprs.setdefault(sid, []).append(nid)
        self.reset()

    def reset(self):
        self.log = []
        self.values = {}
        self.call_marks = []
        self.last_state = None
        self.total_steps = 0
        if not self.prog:
            self.vm = None
            return
        externs = {e[0]: self.make_extern(i, e[0], e[3]) for i, e in enumerate(self.prog.externs)}
        self.vm = VM(self.prog, externs, on_emit=self.on_emit, on_fault=self.on_fault,
                     clock=lambda vm: int(time.time() * 1000))
        self.vm.start()
        self.add_log("sys", "VM started" + (" — initial state '%s'" % self.prog.states[0] if self.prog.states else ""))

    def add_log(self, kind, text):
        self.log.append({"kind": kind, "text": text, "n": self.total_steps})
        if len(self.log) > LOG_LIMIT:
            del self.log[:len(self.log) - LOG_LIMIT]

    def make_extern(self, idx, name, ret):
        def fn(vm, args):
            shown = ", ".join(V.show(a) for a in args)
            if ret == V.NONE_TYPE:
                self.add_log("call", "%s(%s)" % (name, shown))
                return 0
            r = V.zero(ret)
            text = self.extern_values.get(name)
            if text not in (None, ""):
                try:
                    r = V.parse_literal(ret, str(text))
                except ValueError as e:
                    self.add_log("fault", "%s: return value %r is not a %s (%s): returning %s"
                                 % (name, text, V.TYPE_NAMES[ret], e, V.show(r)))
            self.add_log("call", "%s(%s) → %s" % (name, shown, V.show(r)))
            return r.b if isinstance(r, V.Str) and text not in (None, "") else r
        return fn

    def on_emit(self, vm, name, args):
        self.add_log("emit", "emit %s(%s)" % (name, ", ".join(V.show(a) for a in args)))

    def on_fault(self, vm, code, pc):
        self.add_log("fault", "FAULT %d: %s at pc %s" % (code, FAULTS.get(code, "?"), pc))

    def node_by_id(self, nid):
        if nid is None:
            return None
        if not hasattr(self, "_node_index") or self._node_index[0] is not self.ast:
            idx = {}

            def walk(o):
                if isinstance(o, dict):
                    if "id" in o and "type" in o:
                        idx[o["id"]] = o
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(self.ast)
            self._node_index = (self.ast, idx)
        return self._node_index[1].get(nid)

    # -- stepping ----------------------------------------------------------------------
    def stmt_at(self, pc):
        nid = self.dbg.pc_node.get(pc)
        return self.dbg.stmt_of.get(nid, nid)

    def one(self):
        """One VM step with debugger bookkeeping. Returns the VM step result."""
        vm = self.vm
        was_active = vm.active
        pc = vm.pc
        depth = len(vm.frames)
        if was_active and pc in self.dbg.stmt_starts:
            for nid in self.stmt_exprs.get(self.dbg.stmt_starts[pc], []):
                self.values.pop(nid, None)
        r = vm.step()
        self.total_steps += 1
        if vm.state != self.last_state:
            self.last_state = vm.state
            self.add_log("state", "state → %s" % vm.prog.states[vm.state])
        if r == DISPATCHED:
            self.call_marks = []
            if vm.active:
                rt = self.dbg.routine_at(vm.pc)
                self.add_log("dispatch", "→ %s" % (rt["name"] if rt else "?"))
        elif r == RUNNING and pc in self.dbg.value_marks:
            nid = self.dbg.value_marks[pc]
            if len(vm.frames) > depth:          # CALL: value known after return
                self.call_marks.append((nid, depth))
            elif vm.stack:
                self.values[nid] = vm.stack[-1]
        if r == RUNNING and self.call_marks and len(vm.frames) == self.call_marks[-1][1] and len(vm.frames) < depth:
            nid, _ = self.call_marks.pop()
            if vm.stack:
                self.values[nid] = vm.stack[-1]
        if r in (DONE, FAULT):
            self.call_marks = []
        return r

    def at_breakpoint(self):
        vm = self.vm
        if not vm.active or not self.breakpoints:
            return False
        lines = set()
        if vm.pc in self.dbg.stmt_starts:
            lines.add(self.dbg.pc_line.get(vm.pc))
        if vm.steps == 0:  # just dispatched: the handler head counts too
            rt = self.dbg.routine_at(vm.pc)
            node = self.node_by_id(rt["node"]) if rt else None
            if node:
                lines.add(node.get("line"))
        return bool(lines & self.breakpoints)

    def step(self, mode):
        vm = self.vm
        if vm is None:
            return "nocode"
        limit = 200000
        if mode == "instr":
            return self.one()
        if mode in ("stmt", "over", "out"):
            if not vm.active:
                return self.one()
            start_stmt = self.stmt_at(vm.pc)
            depth = len(vm.frames)
            for _ in range(limit):
                r = self.one()
                if r != RUNNING:
                    return r
                d = len(vm.frames)
                if mode == "out":
                    if d < depth:
                        return r
                    continue
                if mode == "over" and d > depth:
                    continue
                if mode == "over" and d < depth:
                    return r
                if self.stmt_at(vm.pc) != start_stmt or vm.pc in self.dbg.stmt_starts or d != depth:
                    return r
            return "limit"
        if mode == "handler":
            if not vm.active:
                r = self.one()
                if r != DISPATCHED:
                    return r
            for _ in range(limit):
                r = self.one()
                if r != RUNNING:
                    return r
            return "limit"
        if mode == "run":
            first = True
            for _ in range(limit):
                if not first and self.at_breakpoint():
                    return "breakpoint"
                first = False
                r = self.one()
                if r == IDLE:
                    return r
            return "limit"
        raise ValueError("unknown step mode %r" % mode)

    # -- snapshot ----------------------------------------------------------------------
    def snapshot(self, status=None):
        vm = self.vm
        if vm is None:
            return {"ready": False, "status": "error", "log": self.log if hasattr(self, "log") else []}
        p = self.prog
        snap = {
            "ready": True,
            "status": status,
            "active": vm.active,
            "pc": vm.pc if vm.active else None,
            "node": self.dbg.pc_node.get(vm.pc) if vm.active else None,
            "line": self.dbg.pc_line.get(vm.pc) if vm.active else None,
            "state": vm.state_name(),
            "pending": None if vm.pending is None else p.states[vm.pending],
            "globals": [{"name": n, "value": jv(v), "type": V.TYPE_NAMES[V.type_of(v)]}
                        for n, v in zip(p.global_names, vm.globals)],
            "stack": [jv(v) for v in vm.stack],
            "frames": [],
            "queue": [{"event": p.events[e][0], "args": [jv(x) for x in a]} for e, a in vm.queue],
            "timers": [],
            "values": {str(k): jv(v) for k, v in self.values.items()},
            "strings_in_use": vm.strings_in_use(),
            "steps": vm.steps if vm.active else 0,
            "total_steps": self.total_steps,
            "log": self.log[-200:],
        }
        if vm.active:
            snap["stmt"] = self.stmt_at(vm.pc)
            for i, fr in enumerate(vm.frames):
                # outer frames are paused at their CALL instruction (3 bytes before return pc)
                fpc = vm.frames[i + 1][0] - 3 if i + 1 < len(vm.frames) else vm.pc
                rt = self.dbg.routine_at(fpc)
                base = fr[1]
                local_vals = [{"name": nm, "value": jv(vm.stack[base + j])}
                              for j, nm in enumerate(rt["locals"] if rt else [])
                              if base + j < len(vm.stack)]
                snap["frames"].append({"routine": rt["name"] if rt else "?", "pc": fpc,
                                       "node": self.dbg.pc_node.get(fpc), "locals": local_vals,
                                       "base": base})
        for i, t in enumerate(vm.timers):
            snap["timers"].append({"name": p.events[p.timers[i]][0], "active": t[0],
                                   "periodic": t[1], "period": t[2], "remaining": t[3],
                                   "owner": None if t[4] is None else p.states[t[4]]})
        return snap

    def compile_result(self):
        res = {"source": self.source, "errors": self.errors, "ast": self.ast,
               "warnings": self.dbg.warnings if self.prog else [],
               "path": self.path, "extern_values": self.extern_values,
               "breakpoints": sorted(self.breakpoints)}
        if self.prog:
            p = self.prog
            local_names = {}
            for r in self.dbg.routines:
                for pc in range(r["start"], r["end"]):
                    local_names[pc] = r["locals"]
            dis = disassemble(p, local_names)
            for ins in dis:
                if isinstance(ins["arg"], float):
                    ins["arg"] = V.fmt_float(ins["arg"])
                ins["node"] = self.dbg.pc_node.get(ins["pc"])
                ins["line"] = self.dbg.pc_line.get(ins["pc"])
                ins["stmt"] = self.dbg.stmt_of.get(ins["node"], ins["node"])
            res.update({
                "disasm": dis,
                "size": len(p.to_bytes()),
                "code_size": len(p.code),
                "events": [{"name": n, "argc": a, "types": [V.TYPE_NAMES[t] for t in ts]}
                           for n, a, ts in p.events[3:]],
                "externs": [{"name": n, "argc": a, "types": [V.TYPE_NAMES[t] for t in ts],
                             "returns": None if r == V.NONE_TYPE else V.TYPE_NAMES[r]}
                            for n, a, ts, r in p.externs],
                "states": list(p.states),
                "timers": [p.events[e][0] for e in p.timers],
                "stmt_of": {str(k): v for k, v in self.dbg.stmt_of.items()},
                "routines": self.dbg.routines,
                "size_report": size_report(p),
            })
        res["snapshot"] = self.snapshot("compiled")
        return res


def make_handler(session, lock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_json(self, obj, code=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/index.html"
            fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
            if not fp.startswith(STATIC) or not os.path.isfile(fp):
                self.send_error(404)
                return
            ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css",
                     "svg": "image/svg+xml"}.get(fp.rsplit(".", 1)[-1], "application/octet-stream")
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self.send_json({"error": "bad json"}, 400)
                return
            route = self.path.split("?", 1)[0]
            with lock:
                try:
                    self.send_json(self.route(route, body))
                except FlowError as e:
                    self.send_json({"error": str(e), "detail": e.to_dict()})
                except Exception as e:  # keep the debugger alive
                    import traceback
                    traceback.print_exc()
                    self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

        def route(self, route, body):
            s = session
            if route == "/api/load":
                return s.compile_result()
            if route == "/api/compile":
                s.compile(body.get("source", ""))
                return s.compile_result()
            if route == "/api/from_model":
                src = to_source(body["ast"])
                s.compile(src)
                return s.compile_result()
            if route == "/api/format":
                return {"source": to_source(parse(body.get("source", "")))}
            if route == "/api/parse_exprs":
                out = []
                for text in body.get("texts", []):
                    try:
                        e = parse_expression(text)
                        out.append({"ok": True, "expr": e, "text": expr_to_str(e)})
                    except FlowError as err:
                        out.append({"ok": False, "error": err.msg})
                return {"results": out}
            if route == "/api/save":
                path = body.get("path") or s.path
                if not path:
                    return {"error": "no file name"}
                with open(path, "w") as f:
                    f.write(body.get("source", s.source))
                s.path = path
                return {"ok": True, "path": path}
            if route == "/api/bytecode":
                if not s.prog:
                    return {"error": "program does not compile"}
                import base64
                return {"base64": base64.b64encode(s.prog.to_bytes(debug=bool(body.get("debug")))).decode()}
            if s.vm is None and route.startswith("/api/vm/"):
                return {"snapshot": s.snapshot("error")}
            if route == "/api/vm/reset":
                s.reset()
                return {"snapshot": s.snapshot("reset")}
            if route == "/api/vm/step":
                r = s.step(body.get("mode", "instr"))
                return {"snapshot": s.snapshot(r)}
            if route == "/api/vm/post":
                ev = s.prog.event_index(body["event"])
                types = s.prog.events[ev][2] if ev >= 0 else ()
                try:
                    args = [V.parse_literal(t, str(a)) for t, a in zip(types, body.get("args", []))]
                except ValueError as e:
                    s.add_log("fault", "post %s: %s" % (body["event"], e))
                    return {"snapshot": s.snapshot("posted")}
                try:
                    ok = s.vm.post(body["event"], *[a.b if isinstance(a, V.Str) else a for a in args])
                except TypeError as e:
                    ok = False
                    s.add_log("fault", "post %s: %s" % (body["event"], e))
                s.add_log("post", "post %s(%s)%s" % (body["event"], ", ".join(V.show(a) for a in args),
                                                     "" if ok else " — not queued"))
                return {"snapshot": s.snapshot("posted")}
            if route == "/api/vm/tick":
                ms = int(body.get("ms", 0))
                s.vm.tick(ms)
                s.add_log("tick", "tick %d ms" % ms)
                return {"snapshot": s.snapshot("ticked")}
            if route == "/api/vm/extern":
                s.extern_values[body["name"]] = str(body.get("value", ""))
                return {"ok": True}
            if route == "/api/vm/breakpoints":
                s.breakpoints = set(int(x) for x in body.get("lines", []))
                return {"ok": True}
            if route == "/api/vm/set_global":
                idx = s.prog.global_names.index(body["name"])
                t = V.type_of(s.vm.globals[idx])
                try:
                    v = V.parse_literal(t, str(body["value"]))
                    s.vm.set_global(idx, v.b if isinstance(v, V.Str) else v)
                except (ValueError, V.ValueFault) as e:
                    s.add_log("fault", "%s: %r is not a %s value (%s)" % (body["name"], body["value"], V.TYPE_NAMES[t], e))
                return {"snapshot": s.snapshot("edited")}
            raise ValueError("unknown route %s" % route)

    return Handler


def serve(path=None, port=8642, open_browser=True):
    session = Session(path)
    if session.errors:
        print("warning: %s" % session.errors[0])
    try:
        httpd = HTTPServer(("127.0.0.1", port), make_handler(session, threading.Lock()))
    except OSError as e:
        import errno
        if e.errno == errno.EADDRINUSE:
            raise SystemExit("error: port %d is already in use (another debugger still running?). "
                             "Stop it, or pick another port with --port N." % port)
        raise
    url = "http://127.0.0.1:%d/" % port
    print("FlowScript debugger on %s  (Ctrl+C to stop)" % url)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
