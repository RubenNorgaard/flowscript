"""FlowScript compiler: AST -> Program (bytecode + debug info)."""

import struct

from .lexer import FlowError
from .parser import parse, COMPARE_OPS
from .bytecode import Program, OPCODES, BUILTIN_EVENTS, EV_BOOT, EV_ENTER, EV_EXIT
from . import intops

MAX_EVENT_ARGS = 8
MAX_LOCALS = 32     # params + locals per routine; must stay well below the VM stack (64)
MAX_PARAMS = 16

BIN_OPS = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
    "&": "BAND", "|": "BOR", "^": "BXOR", "<<": "SHL", ">>": "SHR",
    "==": "EQ", "!=": "NE", "<": "LT", "<=": "LE", ">": "GT", ">=": "GE",
}


class DebugInfo:
    """Rich debug info kept in memory by the toolchain/debugger."""

    def __init__(self):
        self.pc_node = {}        # pc -> node id
        self.pc_line = {}        # pc -> source line
        self.stmt_of = {}        # node id (expr or stmt) -> statement / handler node id
        self.value_marks = {}    # pc -> expr node id (TOS after executing pc is node's value)
        self.stmt_starts = {}    # pc -> statement id (first instruction of a statement)
        self.routines = []       # dicts: start, end, kind, name, state, locals, node
        self.node_lines = {}     # node id -> line
        self.warnings = []       # {"msg", "line", "col"}: compiled anyway

    def routine_at(self, pc):
        for r in self.routines:
            if r["start"] <= pc < r["end"]:
                return r
        return None

    def to_dict(self):
        return {
            "pc_node": self.pc_node, "pc_line": self.pc_line, "stmt_of": self.stmt_of,
            "value_marks": self.value_marks, "stmt_starts": self.stmt_starts,
            "routines": self.routines,
        }


class Routine:
    def __init__(self, kind, name, params):
        self.kind = kind          # "func" or "handler"
        self.name = name
        self.locals = list(params)

    def declare(self, name, node, comp):
        if name in self.locals:
            raise comp.err("'%s' is already declared in this %s" % (name, "function" if self.kind == "func" else "handler"), node)
        if len(self.locals) >= MAX_LOCALS:
            raise comp.err("too many local variables", node)
        self.locals.append(name)
        return len(self.locals) - 1


class Label:
    __slots__ = ("pc", "refs")

    def __init__(self):
        self.pc = None
        self.refs = []


class Compiler:
    def __init__(self, ast):
        self.ast = ast
        self.prog = Program()
        self.dbg = DebugInfo()
        self.code = bytearray()
        self.cur_node = None
        self.cur_stmt = None
        self.routine = None
        # symbol tables
        self.externs = {}   # name -> (index, argc)
        self.events = {}    # name -> (index, argc, params)
        self.timers = {}    # name -> (index, event index)
        self.consts = {}    # name -> value
        self.globals = {}   # name -> index
        self.funcs = {}     # name -> (index, argc, node)
        self.states = {}    # name -> index
        self.strings = {}
        self.any_state = None   # the any_state item (handlers for every state)
        self.state_vars = {}    # state name -> {var name: global index}
        self.cur_state = None   # name of the state whose handler is being compiled

    # -- errors / helpers ------------------------------------------------------
    @staticmethod
    def err(msg, node=None):
        return FlowError(msg, node.get("line", 0) if node else 0, 0)

    def define(self, name, kind, node):
        for table, what in ((self.externs, "extern"), (self.events, "event"),
                            (self.timers, "timer"), (self.consts, "const"),
                            (self.globals, "var"), (self.funcs, "func"), (self.states, "state")):
            if name in table:
                raise self.err("'%s' is already declared as %s" % (name, what), node)
        if name in BUILTIN_EVENTS:
            raise self.err("'%s' is a built-in event name" % name, node)

    def string(self, s):
        if s not in self.strings:
            self.strings[s] = len(self.prog.strings)
            self.prog.strings.append(s)
        return self.strings[s]

    # -- emission ----------------------------------------------------------------
    def emit(self, op, arg=None, node=None):
        code, kind = OPCODES[op]
        pc = len(self.code)
        if pc > 0xFFFF:
            raise self.err("program too large (code > 64 KiB)")
        nd = node if node is not None else self.cur_node
        self.code.append(code)
        if kind == "i32":
            self.code += struct.pack("<i", arg)
        elif kind == "i8":
            self.code += struct.pack("<b", arg)
        elif kind == "u8":
            self.code += struct.pack("<B", arg)
        elif kind == "u16":
            if isinstance(arg, Label):
                arg.refs.append(pc + 1)
                self.code += b"\0\0"
            else:
                self.code += struct.pack("<H", arg)
        if nd is not None:
            self.dbg.pc_node[pc] = nd["id"]
            self.dbg.pc_line[pc] = nd.get("line", 0)
        return pc

    def place(self, label):
        label.pc = len(self.code)
        for ref in label.refs:
            struct.pack_into("<H", self.code, ref, label.pc)

    def finish_labels(self, labels):
        for lb in labels:
            for ref in lb.refs:
                struct.pack_into("<H", self.code, ref, lb.pc)

    def push_const(self, v, node=None):
        v = intops.wrap(v)
        if -128 <= v <= 127:
            self.emit("PUSH8", v, node)
        else:
            self.emit("PUSH", v, node)

    # -- declarations -------------------------------------------------------------
    def collect(self):
        p = self.prog
        p.name = self.ast.get("name")
        p.version = int(self.ast.get("version") or 0)
        for i, n in enumerate(BUILTIN_EVENTS):
            p.events.append((n, 0))
        for item in self.ast["items"]:
            t = item["type"]
            if t == "comment":
                continue
            if t == "extern":
                self.define(item["name"], t, item)
                self.check_params(item["params"], item)
                self.externs[item["name"]] = (len(p.externs), len(item["params"]))
                p.externs.append((item["name"], len(item["params"])))
            elif t == "event":
                self.define(item["name"], t, item)
                params = item.get("params") or []
                self.check_params(params, item)
                if len(params) > MAX_EVENT_ARGS:
                    raise self.err("events can have at most %d parameters" % MAX_EVENT_ARGS, item)
                self.events[item["name"]] = (len(p.events), len(params), params)
                p.events.append((item["name"], len(params)))
            elif t == "timer":
                self.define(item["name"], t, item)
                ev = len(p.events)
                p.events.append((item["name"], 0))
                self.timers[item["name"]] = (len(p.timers), ev)
                p.timers.append(ev)
            elif t == "const":
                self.define(item["name"], t, item)
                self.consts[item["name"]] = self.const_value(item["value"])
            elif t == "var":
                self.define(item["name"], t, item)
                v = self.const_value(item["value"]) if item.get("value") is not None else 0
                self.globals[item["name"]] = len(p.globals)
                p.globals.append(v)
                p.global_names.append(item["name"])
            elif t == "func":
                self.define(item["name"], t, item)
                self.check_params(item["params"], item)
                self.funcs[item["name"]] = (len(self.funcs), len(item["params"]), item)
            elif t == "state":
                self.define(item["name"], t, item)
                self.states[item["name"]] = None   # numbered below: the initial state is 0
            elif t == "any_state":
                if self.any_state is not None:
                    raise self.err("only one 'any_state' block is allowed (first one at line %d)"
                                   % self.any_state["line"], item)
                self.any_state = item
            else:
                raise self.err("unknown declaration type '%s'" % t, item)
        # the initial state gets index 0, the VM's start state; the rest keep source order
        states = [i for i in self.ast["items"] if i["type"] == "state"]
        marked = [s for s in states if s.get("initial")]
        if len(marked) > 1:
            raise self.err("only one state can be 'initial' (already: '%s')" % marked[0]["name"],
                           marked[1])
        if not marked and len(states) > 1:
            raise self.err("mark the state the program starts in: 'initial state %s:'"
                           % states[0]["name"], states[0])
        first = marked[0] if marked else (states[0] if states else None)
        for s in ([first] if first else []) + [s for s in states if s is not first]:
            self.states[s["name"]] = len(p.states)
            p.states.append(s["name"])
        # state variables: after the top level, so any name clash is known
        for item in self.ast["items"]:
            if item["type"] != "state":
                continue
            own = self.state_vars[item["name"]] = {}
            for v in item.get("vars") or []:
                if v["type"] != "var":
                    continue
                self.define(v["name"], "var", v)
                if v["name"] in own:
                    raise self.err("'%s' is already declared in state '%s'"
                                   % (v["name"], item["name"]), v)
                own[v["name"]] = len(p.globals)
                p.globals.append(self.const_value(v["value"]) if v.get("value") is not None else 0)
                p.global_names.append("%s.%s" % (item["name"], v["name"]))

    def global_index(self, name, node=None):
        """Slot of a state variable of the current state, else of a global, else None.

        Functions see no variables at all: data comes in as parameters and goes out
        with 'return' (or 'raise_event'), so only event handlers change program state.
        """
        own = self.state_vars.get(self.cur_state) or {}
        if name in own:
            return own[name]
        if name in self.globals and self.routine.kind == "func":
            raise self.err("functions cannot use global variables: pass '%s' in as a parameter, "
                           "or return a value and let the event handler store it" % name, node)
        return self.globals.get(name)

    def other_state_var(self, name, node):
        owners = [st for st, vs in self.state_vars.items() if name in vs]
        if not owners:
            return None
        return self.err("'%s' is a variable of state %s, only visible in %s handlers"
                        % (name, " and ".join("'%s'" % o for o in owners),
                           "that state's" if len(owners) == 1 else "those states'"), node)

    def check_params(self, params, node):
        if len(params) > MAX_PARAMS:
            raise self.err("too many parameters (max %d)" % MAX_PARAMS, node)
        seen = set()
        for prm in params:
            if prm in seen:
                raise self.err("duplicate parameter '%s'" % prm, node)
            seen.add(prm)

    def const_value(self, e):
        v = self.try_fold(e)
        if v is None:
            raise self.err("value must be a constant expression", e)
        return v

    def try_fold(self, e):
        """Evaluate a constant expression, or return None."""
        t = e["type"]
        if t in ("num", "bool"):
            return intops.wrap(e["value"])
        if t == "name":
            if self.routine and e["name"] in self.routine.locals:
                return None
            return self.consts.get(e["name"])
        if t == "unary":
            v = self.try_fold(e["operand"])
            return None if v is None else intops.UNARY[e["op"]](v)
        if t == "binary":
            a = self.try_fold(e["left"])
            if a is None:
                return None
            b = self.try_fold(e["right"])
            if b is None:
                return None
            try:
                return intops.BINARY[e["op"]](a, b)
            except intops.DivZero:
                raise self.err("division by zero in constant expression", e)
        return None

    # -- program -------------------------------------------------------------------
    def compile(self):
        self.collect()
        func_offsets = {}
        seen_handlers = set()
        for item in self.ast["items"]:
            t = item["type"]
            if t == "func":
                func_offsets[item["name"]] = self.compile_func(item)
            elif t == "any_state":
                for h in item["handlers"]:
                    if h["type"] != "comment":
                        self.compile_handler(h, None, seen_handlers)
            elif t == "state":
                for h in item["handlers"]:
                    if h["type"] == "comment":
                        continue
                    self.compile_handler(h, item, seen_handlers)
                if self.state_vars[item["name"]] and \
                        (item["name"], EV_ENTER) not in seen_handlers:
                    self.compile_reset_handler(item)
        self.shadow_warnings()
        self.magic_number_warnings()
        self.dbg.warnings.sort(key=lambda w: w["line"])
        p = self.prog
        p.funcs = [None] * len(self.funcs)
        for name, (idx, argc, node) in self.funcs.items():
            nlocals = node["_nlocals"]
            p.funcs[idx] = (name, argc, nlocals, func_offsets[name])
            del node["_nlocals"]
        p.code = bytes(self.code)
        p.lines = [(pc, self.dbg.pc_line.get(pc, 0), nid) for pc, nid in sorted(self.dbg.pc_node.items())]
        return p

    def shadow_warnings(self):
        """A state handler silently replaces the any_state handler for the same event."""
        if self.any_state is None:
            return
        anywhere = {h["event"]: h for h in self.any_state["handlers"] if h["type"] == "on"}
        for item in self.ast["items"]:
            if item["type"] != "state":
                continue
            for h in item["handlers"]:
                if h["type"] == "on" and h["event"] in anywhere:
                    self.dbg.warnings.append({
                        "msg": "in state '%s', 'on %s' replaces the any_state handler (line %d), "
                               "which then does not run in this state"
                               % (item["name"], h["event"], anywhere[h["event"]]["line"]),
                        "line": h["line"], "col": 1})

    MAGIC_OK = (-1, 0, 1)    # idioms, not magic: n > 0, x == 1, i != -1

    def magic_number_warnings(self):
        """A literal number compared directly hides its meaning: suggest a named const."""
        from .printer import expr_to_str

        def walk(o):
            if isinstance(o, list):
                for x in o:
                    walk(x)
                return
            if not isinstance(o, dict):
                return
            if o.get("type") == "binary" and o["op"] in COMPARE_OPS:
                for side in (o["left"], o["right"]):
                    if side["type"] == "num" and side["value"] not in self.MAGIC_OK:
                        lit = expr_to_str(side)
                        self.dbg.warnings.append({
                            "msg": "magic number %s in '%s': name it, e.g. 'const NAME = %s'"
                                   % (lit, expr_to_str(o), lit),
                            "line": side.get("line") or o.get("line", 0), "col": 1})
            for k, v in o.items():
                if k != "note" and isinstance(v, (dict, list)):
                    walk(v)

        for item in self.ast["items"]:
            if item["type"] == "func":
                walk(item["body"])
            elif item["type"] in ("state", "any_state"):
                walk(item["handlers"])

    def begin_routine(self, kind, name, params, node, state=None):
        self.routine = Routine(kind, name, params)
        self.cur_stmt = node
        self.cur_node = node
        self.dbg.stmt_of[node["id"]] = node["id"]
        return len(self.code)

    def end_routine(self, start, node, state=None):
        self.dbg.routines.append({
            "start": start, "end": len(self.code), "kind": self.routine.kind,
            "name": self.routine.name, "state": state, "locals": list(self.routine.locals),
            "node": node["id"],
        })
        n = len(self.routine.locals)
        self.routine = None
        return n

    def compile_func(self, f):
        start = self.begin_routine("func", f["name"], f["params"], f)
        self.block(f["body"])
        if not self.ends_flow(f["body"]):
            self.emit("RET0", node=f)
        f["_nlocals"] = self.end_routine(start, f)
        return start

    def compile_handler(self, h, state, seen):
        ev = h["event"]
        sname = state["name"] if state else None
        if ev in ("enter", "exit"):
            if state is None:
                raise self.err("'on %s' is only allowed inside a state (any_state is never "
                               "entered or left)" % ev, h)
            evi, argc, eparams = BUILTIN_EVENTS.index(ev), 0, []
        elif ev == "boot":
            if state is not None:
                raise self.err("'on boot' belongs in any_state", h)
            evi, argc, eparams = EV_BOOT, 0, []
        elif ev in self.events:
            evi, argc, eparams = self.events[ev]
        elif ev in self.timers:
            evi, argc, eparams = self.timers[ev][1], 0, []
        else:
            raise self.err("unknown event '%s' (declare it with 'event %s')" % (ev, ev), h)
        key = (sname, evi)
        if key in seen:
            raise self.err("duplicate handler for '%s'%s" % (ev, (" in state " + sname) if sname else ""), h)
        seen.add(key)
        params = h.get("params")
        if params is None:
            params = list(eparams)
        self.check_params(params, h)
        for prm in params:
            if prm in (self.state_vars.get(sname) or {}):
                raise self.err("parameter '%s' would hide the state variable of the same name" % prm, h)
        if len(params) > argc:
            raise self.err("event '%s' has only %d parameter(s)" % (ev, argc), h)
        label = ("%s.on %s" % (sname, ev)) if sname else ("on " + ev)
        start = self.begin_routine("handler", label, params, h, sname)
        # pad locals so all event args fit (unnamed ones get hidden names)
        while len(self.routine.locals) < argc:
            self.routine.locals.append("$arg%d" % len(self.routine.locals))
        self.cur_state = sname
        if ev == "enter":
            self.reset_state_vars(state)
        self.block(h["body"])
        self.cur_state = None
        if not self.ends_flow(h["body"]):
            self.emit("RET0", node=h)
        nlocals = self.end_routine(start, h, sname)
        self.prog.handlers.append((self.states[sname] if sname else None, evi, argc, nlocals, start))

    def reset_state_vars(self, state):
        """Entering a state sets its variables back to their initial values."""
        for v in state.get("vars") or []:
            if v["type"] != "var":
                continue
            saved = (self.cur_stmt, self.cur_node)
            self.cur_stmt = self.cur_node = v
            self.dbg.stmt_of[v["id"]] = v["id"]
            self.dbg.node_lines[v["id"]] = v.get("line", 0)
            self.dbg.stmt_starts[len(self.code)] = v["id"]
            idx = self.state_vars[state["name"]][v["name"]]
            if v.get("value") is not None:
                self.track(v["value"])
                self.track_subtree(v["value"])
            self.push_const(self.prog.globals[idx], v)
            self.emit("STOREG", idx, v)
            self.cur_stmt, self.cur_node = saved

    def compile_reset_handler(self, state):
        """A state with variables but no 'on enter' gets one that only resets them."""
        sname = state["name"]
        first = next(v for v in state["vars"] if v["type"] == "var")
        start = self.begin_routine("handler", "%s.on enter" % sname, [], first, sname)
        self.reset_state_vars(state)
        self.emit("RET0", node=first)
        nlocals = self.end_routine(start, first, sname)
        self.prog.handlers.append((self.states[sname], EV_ENTER, 0, nlocals, start))

    @staticmethod
    def ends_flow(body):
        real = [s for s in body if s["type"] != "comment"]
        return bool(real) and real[-1]["type"] in ("return", "goto")

    # -- statements ------------------------------------------------------------------
    def block(self, body):
        for s in body or []:
            self.stmt(s)

    def stmt(self, s):
        t = s["type"]
        if t == "comment":
            return
        saved = (self.cur_stmt, self.cur_node)
        self.cur_stmt = s
        self.cur_node = s
        self.dbg.stmt_of[s["id"]] = s["id"]
        self.dbg.node_lines[s["id"]] = s.get("line", 0)
        self.dbg.stmt_starts[len(self.code)] = s["id"]
        getattr(self, "stmt_" + t)(s)
        self.cur_stmt, self.cur_node = saved

    def stmt_assign(self, s):
        name = s["target"]
        self.expr(s["value"])
        self.store(name, s)

    def store(self, name, s):
        if name in self.routine.locals:
            self.emit("STOREL", self.routine.locals.index(name), s)
        elif self.global_index(name, s) is not None:
            self.emit("STOREG", self.global_index(name, s), s)
        elif name in self.consts:
            raise self.err("cannot assign to constant '%s'" % name, s)
        else:
            raise self.other_state_var(name, s) or \
                self.err("unknown variable '%s' (declare it with 'var %s')" % (name, name), s)

    def stmt_local(self, s):
        if s["name"] in (self.state_vars.get(self.cur_state) or {}):
            raise self.err("local '%s' would hide the state variable of the same name" % s["name"], s)
        if s["name"] in self.globals or s["name"] in self.consts:
            raise self.err("local '%s' would hide a global of the same name" % s["name"], s)
        if s.get("value") is not None:
            self.expr(s["value"])
        else:
            self.push_const(0, s)
        idx = self.routine.declare(s["name"], s, self)
        self.emit("STOREL", idx, s)

    def stmt_call(self, s):
        self.call(s, s)
        self.emit("POP", node=s)

    def stmt_if(self, s):
        end = Label()
        labels = [end]
        branches = s["branches"]
        for i, br in enumerate(branches):
            nxt = Label()
            labels.append(nxt)
            self.cjump(br["cond"], nxt, False)
            self.block(br["body"])
            last = (i == len(branches) - 1)
            if not last or s.get("else_body") is not None:
                if not self.ends_flow(br["body"]):
                    self.emit("JMP", end, s)
            self.place(nxt)
        if s.get("else_body") is not None:
            self.block(s["else_body"])
        self.place(end)
        self.finish_labels(labels)

    def stmt_while(self, s):
        top = Label()
        end = Label()
        self.place(top)
        self.dbg.stmt_starts[top.pc] = s["id"]
        self.cjump(s["cond"], end, False)
        self.block(s["body"])
        self.emit("JMP", top, s)
        self.place(end)
        self.finish_labels([top, end])

    def stmt_goto(self, s):
        if s["state"] not in self.states:
            raise self.err("unknown state '%s'" % s["state"], s)
        self.emit("GOTO", self.states[s["state"]], s)

    def stmt_emit(self, s):
        name = s["event"]
        if name in self.timers:
            raise self.err("cannot emit timer event '%s' (use start)" % name, s)
        if name not in self.events:
            raise self.err("unknown event '%s'" % name, s)
        idx, argc, _ = self.events[name]
        args = s.get("args") or []
        if len(args) != argc:
            raise self.err("event '%s' takes %d argument(s), got %d" % (name, argc, len(args)), s)
        for a in args:
            self.expr(a)
        self.emit("EMIT", idx, s)

    def timer_index(self, s):
        if s["timer"] not in self.timers:
            raise self.err("unknown timer '%s' (declare it with 'timer %s')" % (s["timer"], s["timer"]), s)
        return self.timers[s["timer"]][0]

    def stmt_start(self, s):
        idx = self.timer_index(s)
        self.expr(s["ms"])
        self.emit("TEVERY" if s.get("every") else "TSTART", idx, s)

    def stmt_stop(self, s):
        self.emit("TSTOP", self.timer_index(s), s)

    def stmt_return(self, s):
        v = s.get("value")
        if v is None:
            self.emit("RET0", node=s)
            return
        if self.routine.kind == "handler":
            raise self.err("event handlers cannot return a value", s)
        self.expr(v)
        self.emit("RET", node=s)

    # -- expressions -----------------------------------------------------------------
    def mark(self, e, pc):
        self.dbg.value_marks[pc] = e["id"]

    def track(self, e):
        self.dbg.stmt_of[e["id"]] = self.cur_stmt["id"]
        self.dbg.node_lines[e["id"]] = e.get("line", 0)

    def expr(self, e):
        """Compile e in value context."""
        self.track(e)
        saved = self.cur_node
        self.cur_node = e
        folded = self.try_fold(e)
        t = e["type"]
        if folded is not None:
            self.push_const(folded, e)
            self.mark(e, len(self.code) - (2 if -128 <= folded <= 127 else 5))
            self.track_subtree(e)
        elif t == "str":
            pc = self.emit("PUSHS", self.string(e["value"]))
            self.mark(e, pc)
        elif t == "name":
            name = e["name"]
            if name in self.routine.locals:
                pc = self.emit("LOADL", self.routine.locals.index(name))
            elif self.global_index(name, e) is not None:
                pc = self.emit("LOADG", self.global_index(name, e))
            elif name in self.states or name in self.events or name in self.funcs or name in self.externs:
                raise self.err("'%s' is not a value" % name, e)
            else:
                raise self.other_state_var(name, e) or self.err("unknown name '%s'" % name, e)
            self.mark(e, pc)
        elif t in ("callx", "call"):
            pc = self.call(e, e)
            self.mark(e, pc)
        elif t == "unary":
            self.expr(e["operand"])
            op = {"-": "NEG", "~": "BNOT", "not": "NOT"}[e["op"]]
            self.mark(e, self.emit(op))
        elif t == "binary":
            op = e["op"]
            if op in ("and", "or"):
                f, end = Label(), Label()
                self.cjump(e, f, False)
                self.cur_node = e
                self.mark(e, self.emit("PUSH8", 1))
                self.emit("JMP", end)
                self.place(f)
                self.mark(e, self.emit("PUSH8", 0))
                self.place(end)
                self.finish_labels([f, end])
            elif op == "xor":
                self.expr(e["left"])
                self.cur_node = e
                self.emit("BOOL")
                self.expr(e["right"])
                self.cur_node = e
                self.emit("BOOL")
                self.mark(e, self.emit("NE"))
            else:
                self.expr(e["left"])
                self.expr(e["right"])
                self.cur_node = e
                self.mark(e, self.emit(BIN_OPS[op]))
        else:
            raise self.err("unknown expression type '%s'" % t, e)
        self.cur_node = saved

    def track_subtree(self, e):
        for k in ("left", "right", "operand"):
            if isinstance(e.get(k), dict):
                self.track(e[k])
                self.track_subtree(e[k])

    def call(self, e, node):
        name = e["name"]
        args = e.get("args") or []
        if name in self.funcs:
            idx, argc, _ = self.funcs[name]
            op = "CALL"
        elif name in self.externs:
            idx, argc = self.externs[name]
            op = "XCALL"
        else:
            raise self.err("unknown function '%s' (declare host functions with 'extern %s(...)')" % (name, name), e)
        if len(args) != argc:
            raise self.err("'%s' takes %d argument(s), got %d" % (name, argc, len(args)), e)
        for a in args:
            self.expr(a)
        saved = self.cur_node
        self.cur_node = node
        pc = self.emit(op, idx)
        self.cur_node = saved
        return pc

    def cjump(self, e, label, jump_if):
        """Compile a condition: jump to label if truth(e) == jump_if, else fall through."""
        self.track(e)
        t = e["type"]
        folded = self.try_fold(e)
        if folded is not None:
            self.track_subtree(e)
            if bool(folded) == jump_if:
                self.emit("JMP", label, e)
            return
        if t == "unary" and e["op"] == "not":
            self.cjump(e["operand"], label, not jump_if)
            return
        if t == "binary" and e["op"] in ("and", "or"):
            is_and = e["op"] == "and"
            if is_and != jump_if:
                # and/jump-if-false or or/jump-if-true: both operands jump directly
                self.cjump(e["left"], label, jump_if)
                self.cjump(e["right"], label, jump_if)
            else:
                skip = Label()
                self.cjump(e["left"], skip, not jump_if)
                self.cjump(e["right"], label, jump_if)
                self.place(skip)
                self.finish_labels([skip])
            return
        self.expr(e)
        self.emit("JNZ" if jump_if else "JZ", label, e)


def compile_ast(ast):
    c = Compiler(ast)
    try:
        prog = c.compile()
    except RecursionError:
        raise FlowError("expression is nested too deeply")
    return prog, c.dbg


def compile_source(src):
    ast = parse(src)
    prog, dbg = compile_ast(ast)
    return ast, prog, dbg
