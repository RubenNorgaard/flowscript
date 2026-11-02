"""FlowScript compiler: AST -> Program (bytecode + debug info).

Statically typed: every expression has one of the types int, float, str or time
(values.py). An int is promoted to float where a float is expected; nothing else is
converted implicitly (use int(), float(), str(), time()).
"""

import struct

from .lexer import FlowError
from .parser import parse, COMPARE_OPS
from .bytecode import Program, OPCODES, BUILTIN_EVENTS, EV_BOOT, EV_ENTER, EV_EXIT
from . import intops
from . import values as V
from .values import INT, FLOAT, STR, TIME, TYPE_NAMES, TYPE_CODES, NONE_TYPE

VOID = None     # the "type" of a call that returns nothing


def tname(t):
    return "nothing" if t is None else TYPE_NAMES[t]

MAX_EVENT_ARGS = 8
MAX_LOCALS = 32     # params + locals per routine; must stay well below the VM stack (64)
MAX_PARAMS = 16
MAX_RESULTS = 8   # values a function can return (the VMs accept up to 16)

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
    def __init__(self, kind, name, params, types, returns=None):
        self.kind = kind          # "func" or "handler"
        self.name = name
        self.locals = list(params)
        self.types = list(types)  # type code per local slot
        self.declared = set(params)
        self.returns = returns    # functions: None, a type code, or a tuple of them (several results)

    def declare(self, name, t, node, comp):
        if name in self.locals:
            raise comp.err("'%s' is already declared in this %s" % (name, "function" if self.kind == "func" else "handler"), node)
        if len(self.locals) >= MAX_LOCALS:
            raise comp.err("too many local variables", node)
        self.locals.append(name)
        self.types.append(t)
        return len(self.locals) - 1

    def visible(self, name):
        return name in self.declared


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
        self.externs = {}   # name -> (index, argc, param types, return type or None)
        self.events = {}    # name -> (index, argc, params, param types)
        self.timers = {}    # name -> (index, event index)
        self.consts = {}    # name -> value (int, float, Str, Time)
        self.globals = {}   # name -> index (type: self.global_types[index])
        self.global_types = []
        self.funcs = {}     # name -> (index, argc, node, param types, return type or None)
        self.states = {}    # name -> index
        self.strings = {}
        self.in_boot = False    # compiling 'on boot' (runs before the initial state is entered)
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
        if isinstance(s, bytes):
            s = s.decode("utf-8", "surrogateescape")
        if s not in self.strings:
            self.strings[s] = len(self.prog.strings)
            self.prog.strings.append(s)
        return self.strings[s]

    def type_code(self, name, node):
        if name not in TYPE_CODES:
            raise self.err("unknown type '%s'" % name, node)
        return TYPE_CODES[name]

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
        elif kind == "f64":
            self.code += struct.pack("<d", arg)
        elif kind == "i64":
            self.code += struct.pack("<q", arg)
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
        """Push a constant value; returns the pc of the push."""
        t = V.type_of(v)
        if t == INT:
            v = intops.wrap(v)
            return self.emit("PUSH8" if -128 <= v <= 127 else "PUSH", v, node)
        if t == FLOAT:
            if v == int(v) if v == v and abs(v) < 2 ** 31 else False:
                if not (v == 0 and str(v).startswith("-")):   # keep -0.0 exact
                    iv = int(v)
                    self.emit("PUSH8" if -128 <= iv <= 127 else "PUSH", iv, node)
                    return self.emit("BUILTIN", 0, node)      # float(int): 4-7 bytes instead of 9
            return self.emit("PUSHF", v, node)
        if t == STR:
            return self.emit("PUSHS", self.string(v.b), node)
        return self.emit("PUSHT", v.ms, node)

    def convert_const(self, v, want, node, what="value"):
        """A constant converted to type `want` (int -> float only)."""
        t = V.type_of(v)
        if want is None or t == want:
            return v
        if t == INT and want == FLOAT:
            return float(v)
        raise self.err("%s is %s, expected %s" % (what, TYPE_NAMES[t], TYPE_NAMES[want]), node)

    # -- declarations -------------------------------------------------------------
    def collect(self):
        p = self.prog
        p.name = self.ast.get("name")
        p.version = int(self.ast.get("version") or 0)
        for i, n in enumerate(BUILTIN_EVENTS):
            p.events.append((n, 0, ()))
        for item in self.ast["items"]:
            t = item["type"]
            if t == "comment":
                continue
            if t == "extern":
                self.define(item["name"], t, item)
                self.check_params(item["params"], item)
                ptypes = self.param_types(item)
                ret = self.type_code(item["returns"], item) if item.get("returns") else None
                self.externs[item["name"]] = (len(p.externs), len(item["params"]), ptypes, ret)
                p.externs.append((item["name"], len(item["params"]), tuple(ptypes),
                                  NONE_TYPE if ret is None else ret))
            elif t == "event":
                self.define(item["name"], t, item)
                params = item.get("params") or []
                self.check_params(params, item)
                if len(params) > MAX_EVENT_ARGS:
                    raise self.err("events can have at most %d parameters" % MAX_EVENT_ARGS, item)
                ptypes = self.param_types(item)
                self.events[item["name"]] = (len(p.events), len(params), params, ptypes)
                p.events.append((item["name"], len(params), tuple(ptypes)))
            elif t == "timer":
                self.define(item["name"], t, item)
                ev = len(p.events)
                p.events.append((item["name"], 0, ()))
                self.timers[item["name"]] = (len(p.timers), ev)
                p.timers.append(ev)
            elif t == "const":
                self.define(item["name"], t, item)
                self.consts[item["name"]] = self.const_value(item["value"])
            elif t == "var":
                self.define(item["name"], t, item)
                self.globals[item["name"]] = len(p.globals)
                p.globals.append(self.initial_value(item))
                self.global_types.append(self.type_code(item.get("vtype") or "int", item))
                p.global_names.append(item["name"])
            elif t == "func":
                self.define(item["name"], t, item)
                self.check_params(item["params"], item)
                ret = self.result_types(item)
                self.funcs[item["name"]] = (len(self.funcs), len(item["params"]), item,
                                            self.param_types(item), ret)
            elif t == "state":
                self.define(item["name"], t, item)
                self.states[item["name"]] = None   # numbered below: the initial state is 0
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
                p.globals.append(self.initial_value(v))
                self.global_types.append(self.type_code(v.get("vtype") or "int", v))
                p.global_names.append("%s.%s" % (item["name"], v["name"]))

    def result_types(self, f):
        """A function's results: None, one type code, or a tuple of codes ('-> float, int')."""
        if not f.get("returns"):
            return None
        codes = tuple(self.type_code(t.strip(), f) for t in f["returns"].split(","))
        if len(codes) > MAX_RESULTS:
            raise self.err("a function returns at most %d values" % MAX_RESULTS, f)
        return codes[0] if len(codes) == 1 else codes

    @staticmethod
    def n_results(ret):
        return 0 if ret is None else len(ret) if isinstance(ret, tuple) else 1

    def global_index(self, name, node=None):
        """Slot of a state variable of the current state, else of a global, else None.

        Functions see no variables at all: data comes in as parameters and goes out
        with 'return' (or 'raise_event'), so only event handlers change program state.
        """
        own = self.state_vars.get(self.cur_state) or {}
        if name in own:
            if self.in_boot:
                raise self.err("'on boot' runs before '%s' is entered, and entering resets its variables: "
                               "use '%s' in 'on enter' instead" % (self.cur_state, name), node)
            return own[name]
        if name in self.globals and self.routine.kind == "func":
            raise self.err("functions cannot use global variables: pass '%s' in as a parameter, "
                           "or return a value and let the event handler store it" % name, node)
        return self.globals.get(name)

    def var_type(self, name, node):
        """Type of a readable variable/constant name, else raise."""
        r = self.routine
        if name in r.locals and r.visible(name):
            return r.types[r.locals.index(name)]
        gi = self.global_index(name, node)
        if gi is not None:
            return self.global_types[gi]
        if name in self.consts:
            return V.type_of(self.consts[name])
        if name in self.states or name in self.events or name in self.funcs or name in self.externs:
            raise self.err("'%s' is not a value" % name, node)
        raise self.other_state_var(name, node) or self.err("unknown name '%s'" % name, node)

    def other_state_var(self, name, node):
        owners = [st for st, vs in self.state_vars.items() if name in vs]
        if not owners:
            return None
        return self.err("'%s' is a variable of state %s, only visible in %s handlers"
                        % (name, " and ".join("'%s'" % o for o in owners),
                           "that state's" if len(owners) == 1 else "those states'"), node)

    def param_types(self, item):
        types = item.get("ptypes")
        if types is None:      # written without types: int (also the AST of older tools)
            types = ["int"] * len(item.get("params") or [])
        return [self.type_code(t, item) for t in types]

    def initial_value(self, item):
        t = self.type_code(item.get("vtype") or "int", item)
        if item.get("value") is None:
            return V.zero(t)
        return self.convert_const(self.const_value(item["value"]), t, item["value"],
                                  "the initial value of '%s'" % item["name"])

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
        """Evaluate a constant expression (a typed value), or return None."""
        t = e["type"]
        if t == "num":
            v = e["value"]
            return v if isinstance(v, float) else intops.wrap(v)
        if t == "bool":
            return intops.wrap(e["value"])
        if t == "str":
            return V.Str(e["value"])
        if t == "name":
            if self.routine and e["name"] in self.routine.locals:
                return None
            return self.consts.get(e["name"])
        if t == "unary":
            v = self.try_fold(e["operand"])
            if v is None:
                return None
            vt = V.type_of(v)
            if e["op"] == "-" and vt == FLOAT:
                return -v
            if vt != INT:
                return None          # a type error: reported when compiled
            return intops.UNARY[e["op"]](v)
        if t == "binary":
            a = self.try_fold(e["left"])
            if a is None:
                return None
            b = self.try_fold(e["right"])
            if b is None:
                return None
            op = e["op"]
            ta, tb = V.type_of(a), V.type_of(b)
            if op in ("and", "or", "xor"):
                return intops.BINARY[op](a, b) if ta == tb == INT else None
            rt = V.binary_type(op, ta, tb)
            if rt is None:
                return None
            if rt[1]:
                a = float(a)
            if rt[2]:
                b = float(b)
            try:
                r = V.binary(op, a, b)
            except intops.DivZero:
                raise self.err("division by zero in constant expression", e)
            return V.Str(r) if isinstance(r, bytes) else r
        if t == "callx" and e["name"] not in self.funcs and e["name"] not in self.externs:
            args = []
            for a in e.get("args") or []:
                v = self.try_fold(a)
                if v is None:
                    return None
                args.append(v)
            hit = self.resolve_builtin(e["name"], [V.type_of(a) for a in args], e, quiet=True)
            if hit is None:
                return None
            idx, ptypes, _ret = hit
            if idx == "identity":
                return args[0]
            if V.BUILTINS[idx][0] in V.IMPURE:
                return None
            args = [float(a) if pt == FLOAT and V.type_of(a) == INT else a for a, pt in zip(args, ptypes)]
            try:
                r = V.call_builtin(idx, args)
            except V.ValueFault:
                raise self.err("invalid value in constant expression '%s'" % e["name"], e)
            return V.Str(r) if isinstance(r, bytes) else r
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
            elif t == "state":
                for h in item["handlers"]:
                    if h["type"] == "comment":
                        continue
                    self.compile_handler(h, item, seen_handlers)
                if self.state_vars[item["name"]] and \
                        (item["name"], EV_ENTER) not in seen_handlers:
                    self.compile_reset_handler(item)
        self.magic_number_warnings()
        self.dbg.warnings.sort(key=lambda w: w["line"])
        p = self.prog
        p.funcs = [None] * len(self.funcs)
        for name, (idx, argc, node, _pt, _ret) in self.funcs.items():
            nlocals = node["_nlocals"]
            p.funcs[idx] = (name, argc, nlocals, func_offsets[name])
            del node["_nlocals"]
        p.code = bytes(self.code)
        p.lines = [(pc, self.dbg.pc_line.get(pc, 0), nid) for pc, nid in sorted(self.dbg.pc_node.items())]
        return p

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
            elif item["type"] == "state":
                walk(item["handlers"])

    def begin_routine(self, kind, name, params, types, node, state=None, returns=None):
        self.routine = Routine(kind, name, params, types, returns)
        self.cur_stmt = node
        self.cur_node = node
        self.dbg.stmt_of[node["id"]] = node["id"]
        return len(self.code)

    def predeclare(self, body, node):
        """Locals are routine-scoped and start at their type's zero value: give every
        declaration in the body its slot now, and initialise the non-int ones (a stack slot
        starts as int 0), so a local declared in a branch that did not run still has a value
        of its type."""
        found = []

        def walk(stmts, nested):
            for st in stmts or []:
                if st["type"] == "local":
                    found.append((st, nested))
                elif st["type"] == "if":
                    for br in st["branches"]:
                        walk(br["body"], True)
                    walk(st.get("else_body"), True)
                elif st["type"] == "while":
                    walk(st["body"], True)
        walk(body, False)
        for st, nested in found:
            if st["name"] in (self.state_vars.get(self.cur_state) or {}):
                raise self.err("local '%s' would hide the state variable of the same name" % st["name"], st)
            if st["name"] in self.globals or st["name"] in self.consts:
                raise self.err("local '%s' would hide a global of the same name" % st["name"], st)
            t = self.type_code(st.get("vtype") or "int", st)
            idx = self.routine.declare(st["name"], t, st, self)
            # a declaration at the top of the body runs before any code that can see it
            if t != INT and nested:
                self.push_const(V.zero(t), node)
                self.emit("STOREL", idx, node)

    def end_routine(self, start, node, state=None):
        self.dbg.routines.append({
            "start": start, "end": len(self.code), "kind": self.routine.kind,
            "name": self.routine.name, "state": state, "locals": list(self.routine.locals),
            "types": [TYPE_NAMES[t] for t in self.routine.types], "node": node["id"],
        })
        n = len(self.routine.locals)
        self.routine = None
        return n

    def compile_func(self, f):
        _idx, _argc, _node, ptypes, ret = self.funcs[f["name"]]
        start = self.begin_routine("func", f["name"], f["params"], ptypes, f, returns=ret)
        self.predeclare(f["body"], f)
        self.block(f["body"])
        if not self.ends_flow(f["body"]):
            if isinstance(ret, tuple):    # falling off the end returns the zero value of each result
                for t in ret:
                    self.push_const(V.zero(t), f)
                self.emit("RETN", len(ret), node=f)
            elif ret is None or ret == INT:
                self.emit("RET0", node=f)
            else:               # falling off the end returns the zero value of the type
                self.push_const(V.zero(ret), f)
                self.emit("RET", node=f)
        f["_nlocals"] = self.end_routine(start, f)
        return start

    def compile_handler(self, h, state, seen):
        ev = h["event"]
        sname = state["name"] if state else None
        if ev in ("enter", "exit"):
            evi, argc, eparams, etypes = BUILTIN_EVENTS.index(ev), 0, [], []
        elif ev == "boot":
            if self.states[sname] != 0:
                initial = next(n for n, i in self.states.items() if i == 0)
                raise self.err("'on boot' goes in the initial state ('%s'): it runs once at start, "
                               "before that state is entered" % initial, h)
            evi, argc, eparams, etypes = EV_BOOT, 0, [], []
        elif ev in self.events:
            evi, argc, eparams, etypes = self.events[ev]
        elif ev in self.timers:
            evi, argc, eparams, etypes = self.timers[ev][1], 0, [], []
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
        start = self.begin_routine("handler", label, params, etypes[:len(params)], h, sname)
        # pad locals so all event args fit (unnamed ones get hidden names)
        while len(self.routine.locals) < argc:
            self.routine.types.append(etypes[len(self.routine.locals)])
            self.routine.locals.append("$arg%d" % len(self.routine.locals))
        self.cur_state = sname
        self.in_boot = ev == "boot"
        self.predeclare(h["body"], h)
        if ev == "enter":
            self.reset_state_vars(state)
        self.block(h["body"])
        self.cur_state = None
        self.in_boot = False
        if not self.ends_flow(h["body"]):
            self.emit("RET0", node=h)
        nlocals = self.end_routine(start, h, sname)
        self.prog.handlers.append((self.states[sname], evi, argc, nlocals, start))

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
        start = self.begin_routine("handler", "%s.on enter" % sname, [], [], first, sname)
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
        self.store(name, s, self.target_type(name, s), s["value"])

    def target_type(self, name, s):
        r = self.routine
        if name in r.locals and r.visible(name):
            return r.types[r.locals.index(name)]
        gi = self.global_index(name, s)
        if gi is not None:
            return self.global_types[gi]
        if name in self.consts:
            raise self.err("cannot assign to constant '%s'" % name, s)
        raise self.other_state_var(name, s) or \
            self.err("unknown variable '%s' (declare it with a type, e.g. 'int %s = 0')" % (name, name), s)

    def store(self, name, s, t, value):
        self.expr(value, t, "'%s' is %s:" % (name, TYPE_NAMES[t]))
        r = self.routine
        if name in r.locals and r.visible(name):
            self.emit("STOREL", r.locals.index(name), s)
        else:
            self.emit("STOREG", self.global_index(name, s), s)

    def stmt_local(self, s):
        idx = self.routine.locals.index(s["name"])
        t = self.routine.types[idx]
        if s.get("value") is not None:
            self.expr(s["value"], t, "'%s' is %s:" % (s["name"], TYPE_NAMES[t]))
        else:
            self.push_const(V.zero(t), s)
        self.routine.declared.add(s["name"])
        self.emit("STOREL", idx, s)

    LOG_OPS = {"debug": "LOGD", "info": "LOGI", "warning": "LOGW", "error": "LOGE"}

    def stmt_log(self, s):
        """warning "text {expr} ...": the values go to the host with the message template.
        The VM never builds the text (it works without run-time strings or float formatting)."""
        pieces, _texts = V.split_template(s["text"])
        args = s.get("args") or []
        if len(args) > V.MAX_LOG_ARGS:
            raise self.err("a message can show at most %d values" % V.MAX_LOG_ARGS, s)
        for e in args:
            self.value_type(e)                        # any type, but it must be a value
            self.expr(e)
        self.emit(self.LOG_OPS[s["level"]], self.string(V.log_template(pieces).encode("utf-8", "surrogateescape")), s)

    def stmt_call(self, s):
        _kind, _idx, _pt, ret = self.call_target(s)
        self.call(s, s)
        for _ in range(max(1, self.n_results(ret))):    # a call without results pushes int 0
            self.emit("POP", node=s)

    def stmt_massign(self, s):
        """a, b, c = f(...): the results of a function, assigned in order."""
        call = s["value"]
        targets = s["targets"]
        if call.get("name") not in self.funcs:
            raise self.err("'%s' is not a function of this program: only a func with several results "
                           "can be assigned to several variables" % call.get("name"), s)
        _kind, _idx, _pt, ret = self.call_target(call)
        n = self.n_results(ret)
        if n != len(targets):
            raise self.err("'%s' returns %d value%s, but %d variable%s are assigned"
                           % (call["name"], n, "" if n == 1 else "s", len(targets), "" if len(targets) == 1 else "s"), s)
        dup = next((t for i, t in enumerate(targets) if t in targets[:i]), None)
        if dup:
            raise self.err("'%s' is assigned twice" % dup, s)
        ttypes = [self.target_type(t, s) for t in targets]
        self.track(call)
        self.call(call, s)
        r = self.routine
        for name, tt, rt in reversed(list(zip(targets, ttypes, ret))):     # the last result is on top
            if rt != tt:
                if rt == INT and tt == FLOAT:
                    self.emit("BUILTIN", 0, node=s)                       # float(int)
                else:
                    raise self.err("'%s' is %s, but result %d of %s() is %s"
                                   % (name, TYPE_NAMES[tt], targets.index(name) + 1, call["name"],
                                      TYPE_NAMES[rt]), s)
            if name in r.locals and r.visible(name):
                self.emit("STOREL", r.locals.index(name), s)
            else:
                self.emit("STOREG", self.global_index(name, s), s)

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
        idx, argc, params, ptypes = self.events[name]
        args = s.get("args") or []
        if len(args) != argc:
            raise self.err("event '%s' takes %d argument(s), got %d" % (name, argc, len(args)), s)
        for a, prm, pt in zip(args, params, ptypes):
            self.expr(a, pt, "%s(%s %s):" % (name, TYPE_NAMES[pt], prm))
        self.emit("EMIT", idx, s)

    def timer_index(self, s):
        if s["timer"] not in self.timers:
            raise self.err("unknown timer '%s' (declare it with 'timer %s')" % (s["timer"], s["timer"]), s)
        return self.timers[s["timer"]][0]

    def stmt_start(self, s):
        idx = self.timer_index(s)
        self.expr(s["ms"], INT, "the timer duration (ms):")
        self.emit("TEVERY" if s.get("every") else "TSTART", idx, s)

    def stmt_stop(self, s):
        self.emit("TSTOP", self.timer_index(s), s)

    def stmt_return(self, s):
        v = s.get("value")
        ret = self.routine.returns
        if s.get("values"):
            if self.routine.kind == "handler":
                raise self.err("event handlers cannot return a value", s)
            n = self.n_results(ret)
            if not isinstance(ret, tuple) or n != len(s["values"]):
                raise self.err("'%s' returns %d value%s, not %d" % (self.routine.name, n, "" if n == 1 else "s",
                                                                    len(s["values"])), s)
            for i, (e, t) in enumerate(zip(s["values"], ret)):
                self.expr(e, t, "result %d of '%s' is %s:" % (i + 1, self.routine.name, TYPE_NAMES[t]))
            self.emit("RETN", len(ret), node=s)
            return
        if isinstance(ret, tuple):
            raise self.err("'%s' returns %d values: return %s" % (self.routine.name, len(ret),
                                                              ", ".join("..." for _ in ret)), s)
        if v is None:
            if self.routine.kind == "func" and ret is not None:
                raise self.err("'%s' must return a %s value" % (self.routine.name, TYPE_NAMES[ret]), s)
            self.emit("RET0", node=s)
            return
        if self.routine.kind == "handler":
            raise self.err("event handlers cannot return a value", s)
        if self.routine.returns is None:
            raise self.err("'%s' returns nothing: declare its type, e.g. 'func %s(...) -> int:'"
                           % (self.routine.name, self.routine.name), s)
        self.expr(v, self.routine.returns, "'%s' returns %s:" % (self.routine.name, TYPE_NAMES[self.routine.returns]))
        self.emit("RET", node=s)

    # -- expressions -----------------------------------------------------------------
    def mark(self, e, pc):
        self.dbg.value_marks[pc] = e["id"]

    def track(self, e):
        self.dbg.stmt_of[e["id"]] = self.cur_stmt["id"]
        self.dbg.node_lines[e["id"]] = e.get("line", 0)

    def typeof(self, e):
        """Static type of an expression (INT, FLOAT, STR, TIME or VOID); raises on errors."""
        t = e["type"]
        if t == "num":
            return FLOAT if isinstance(e["value"], float) else INT
        if t == "bool":
            return INT
        if t == "str":
            return STR
        if t == "name":
            return self.var_type(e["name"], e)
        if t in ("callx", "call"):
            ret = self.call_target(e)[3]
            if isinstance(ret, tuple):
                raise self.err("'%s' returns %d values: assign them with %s = %s(...)"
                               % (e["name"], len(ret), ", ".join("abcdefgh"[i] for i in range(len(ret))),
                                  e["name"]), e)
            return ret
        if t == "unary":
            ot = self.value_type(e["operand"])
            if e["op"] == "-" and ot in (INT, FLOAT):
                return ot
            if ot != INT:
                raise self.err("'%s' needs an int operand, not %s" % (e["op"], TYPE_NAMES[ot]), e)
            return INT
        if t == "binary":
            op = e["op"]
            lt, rt = self.value_type(e["left"]), self.value_type(e["right"])
            if op in ("and", "or", "xor"):
                for side, st in ((e["left"], lt), (e["right"], rt)):
                    if st != INT:
                        raise self.err("'%s' needs conditions (int), not %s: compare it, e.g. x != 0"
                                       % (op, TYPE_NAMES[st]), side)
                return INT
            r = V.binary_type(op, lt, rt)
            if r is None:
                raise self.err("cannot use '%s' on %s and %s" % (op, TYPE_NAMES[lt], TYPE_NAMES[rt]), e)
            return r[0]
        raise self.err("unknown expression type '%s'" % t, e)

    def value_type(self, e):
        t = self.typeof(e)
        if t is VOID:
            raise self.err("'%s' returns nothing, so it has no value" % e.get("name", "?"), e)
        return t

    def expr(self, e, want=None, what="value"):
        """Compile e in value context; convert it to type `want` (int -> float) if given.
        Returns the type of the value left on the stack."""
        t = self.value_type(e)
        if want is not None and t != want and not (t == INT and want == FLOAT):
            raise self.err("%s got %s, expected %s" % (what, TYPE_NAMES[t], TYPE_NAMES[want]), e)
        self.track(e)
        saved = self.cur_node
        self.cur_node = e
        folded = self.try_fold(e)
        if folded is not None:
            if want == FLOAT and V.type_of(folded) == INT:
                folded = float(folded)
                t = FLOAT
            self.mark(e, self.push_const(folded, e))
            self.track_subtree(e)
            self.cur_node = saved
            return t
        self.expr_code(e, t)
        self.cur_node = saved
        if want == FLOAT and t == INT:
            self.promote(e)
            t = FLOAT
        return t

    def promote(self, e):
        self.emit("BUILTIN", 0, e)        # float(int)

    def expr_code(self, e, t):
        et = e["type"]
        if et == "str":
            self.mark(e, self.emit("PUSHS", self.string(e["value"])))
        elif et == "name":
            name = e["name"]
            r = self.routine
            if name in r.locals and r.visible(name):
                pc = self.emit("LOADL", r.locals.index(name))
            else:
                pc = self.emit("LOADG", self.global_index(name, e))
            self.mark(e, pc)
        elif et in ("callx", "call"):
            self.mark(e, self.call(e, e))
        elif et == "unary":
            self.expr(e["operand"])
            op = {"-": "NEG", "~": "BNOT", "not": "NOT"}[e["op"]]
            self.mark(e, self.emit(op))
        elif et == "binary":
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
                rt, pl, pr = V.binary_type(op, self.value_type(e["left"]), self.value_type(e["right"]))
                self.expr(e["left"], FLOAT if pl else None)
                self.expr(e["right"], FLOAT if pr else None)
                self.cur_node = e
                self.mark(e, self.emit("CONCAT" if op == "+" and rt == STR else BIN_OPS[op]))
        else:
            raise self.err("unknown expression type '%s'" % et, e)

    def track_subtree(self, e):
        for k in ("left", "right", "operand"):
            if isinstance(e.get(k), dict):
                self.track(e[k])
                self.track_subtree(e[k])
        for a in e.get("args") or []:
            if isinstance(a, dict):
                self.track(a)
                self.track_subtree(a)

    def resolve_builtin(self, name, argtypes, node, quiet=False):
        """-> (builtin id or "identity", param types, return type), None if not a builtin."""
        cands = [(i, b) for i, b in enumerate(V.BUILTINS) if b[0] == name]
        if not cands:
            return None
        if len(argtypes) == 1 and (name, argtypes[0]) in V.IDENTITY:
            return "identity", (argtypes[0],), argtypes[0]
        exact = [c for c in cands if list(c[1][1]) == list(argtypes)]
        if exact:
            i, b = exact[0]
            return i, b[1], b[2]
        loose = [c for c in cands if len(c[1][1]) == len(argtypes) and all(
            a == p or (a == INT and p == FLOAT) for a, p in zip(argtypes, c[1][1]))]
        if loose:
            i, b = loose[0]
            return i, b[1], b[2]
        if quiet:
            return None
        sigs = ", ".join("%s(%s)" % (name, ", ".join(TYPE_NAMES[p] for p in b[1])) for _i, b in cands)
        raise self.err("no %s() takes (%s); there is %s" % (name, ", ".join(TYPE_NAMES[a] for a in argtypes), sigs), node)

    def call_target(self, e):
        """-> (kind, index, param types, return type) for a call expression/statement."""
        name = e["name"]
        args = e.get("args") or []
        if name in self.funcs:
            idx, argc, _node, ptypes, ret = self.funcs[name]
            kind = "CALL"
        elif name in self.externs:
            idx, argc, ptypes, ret = self.externs[name]
            kind = "XCALL"
        else:
            hit = self.resolve_builtin(name, [self.value_type(a) for a in args], e)
            if hit is None:
                if name in V.LOG_LEVELS:
                    raise self.err("to log, write %s \"message {value}\" without parentheses" % name, e)
                raise self.err("unknown function '%s' (declare host functions with 'extern %s(...)')" % (name, name), e)
            return ("BUILTIN", hit[0], hit[1], hit[2])
        if len(args) != argc:
            raise self.err("'%s' takes %d argument(s), got %d" % (name, argc, len(args)), e)
        return kind, idx, ptypes, ret

    def call(self, e, node):
        kind, idx, ptypes, _ret = self.call_target(e)
        args = e.get("args") or []
        for i, (a, pt) in enumerate(zip(args, ptypes)):
            self.expr(a, pt, "argument %d of %s() (%s):" % (i + 1, e["name"], TYPE_NAMES[pt]))
        saved = self.cur_node
        self.cur_node = node
        if idx == "identity":
            pc = len(self.code) - 1 if self.code else 0
        else:
            pc = self.emit(kind, idx)
        self.cur_node = saved
        return pc

    def cjump(self, e, label, jump_if):
        """Compile a condition: jump to label if truth(e) == jump_if, else fall through."""
        self.track(e)
        ct = self.value_type(e)
        if ct != INT:
            raise self.err("a condition must be an int (true/false), not %s: compare it, e.g. x != 0"
                           % TYPE_NAMES[ct], e)
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
