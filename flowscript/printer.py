"""AST (visual model) -> canonical FlowScript source."""

from .parser import BINARY_LEVELS, COMPARE_OPS

INDENT = "  "

_LEVEL = {}
for _i, _ops in enumerate(BINARY_LEVELS):
    if _ops:
        for _op in _ops:
            _LEVEL[_op] = _i
NOT_LEVEL = 3
UNARY_LEVEL = len(BINARY_LEVELS)
ATOM_LEVEL = 100


def prec(e):
    t = e["type"]
    if t == "binary":
        return _LEVEL[e["op"]]
    if t == "unary":
        return NOT_LEVEL if e["op"] == "not" else UNARY_LEVEL
    if t == "num" and (e["value"] < 0 or str(e["value"]).startswith("-")):
        return UNARY_LEVEL
    return ATOM_LEVEL


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def _num_text(e):
    text = e.get("text")
    if text:
        try:
            from .lexer import tokenize
            toks = [t for t in tokenize(text.lstrip("-")) if t.kind in ("NUM", "FLOAT")]
            v = toks[0].value if toks else None
            if text.startswith("-") and v is not None:
                v = -v
            if v == e["value"]:
                return text
        except Exception:
            pass
    v = e["value"]
    if isinstance(v, float):
        r = repr(v)
        return r if ("e" in r or "." in r or "n" in r) else r + ".0"
    return str(v)


def expr_to_str(e):
    t = e["type"]
    if t == "num":
        return _num_text(e)
    if t == "bool":
        return "true" if e["value"] else "false"
    if t == "str":
        return _quote(e["value"])
    if t == "name":
        return e["name"]
    if t in ("callx", "call"):
        return "%s(%s)" % (e["name"], ", ".join(expr_to_str(a) for a in e["args"]))
    if t == "unary":
        op = e["op"]
        inner = e["operand"]
        s = expr_to_str(inner)
        need = NOT_LEVEL if op == "not" else UNARY_LEVEL
        if prec(inner) < need:
            s = "(" + s + ")"
        return ("not " + s) if op == "not" else (op + s)
    if t == "binary":
        lvl = _LEVEL[e["op"]]
        ls = expr_to_str(e["left"])
        rs = expr_to_str(e["right"])
        lmin = lvl + 1 if e["op"] in COMPARE_OPS else lvl
        if prec(e["left"]) < lmin:
            ls = "(" + ls + ")"
        if prec(e["right"]) <= lvl:
            rs = "(" + rs + ")"
        return "%s %s %s" % (ls, e["op"], rs)
    raise ValueError("unknown expression type %r" % t)


class Printer:
    def __init__(self):
        self.lines = []

    def emit(self, depth, text):
        self.lines.append(INDENT * depth + text if text else "")

    def blank(self):
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def note(self, depth, node):
        n = node.get("note")
        if n:
            for ln in str(n).split("\n"):
                self.emit(depth, ("# " + ln) if ln else "#")

    @staticmethod
    def params(ps, types=None):
        if types:
            ps = ["%s %s" % (t, p) for t, p in zip(types, ps)]
        return "(" + ", ".join(ps) + ")"

    @staticmethod
    def returns(it):
        return (" -> " + it["returns"]) if it.get("returns") else ""

    @staticmethod
    def decl(it):
        v = it.get("value")
        return "%s %s%s" % (it.get("vtype") or "int", it["name"], (" = " + expr_to_str(v)) if v is not None else "")

    def program(self, prog):
        for c in prog.get("pre") or []:
            self.note(0, {"note": c.get("text", "")})
            self.blank()
        self.note(0, prog)
        if prog.get("name"):
            v = prog.get("version") or 0
            self.emit(0, "flow %s%s" % (prog["name"], (" version %d" % v) if v else ""))
        prev = None
        for item in prog.get("items", []):
            kind = item["type"]
            block = kind in ("func", "on", "state")
            if self.lines and (block or prev is None or prev in ("func", "on", "state") or prev != kind):
                self.blank()
            self.item(0, item)
            prev = kind
        return "\n".join(self.lines) + "\n"

    def item(self, d, it):
        t = it["type"]
        if t == "comment":
            self.note(d, {"note": it.get("text", "")})
            self.blank()
            return
        self.note(d, it)
        if t == "extern":
            self.emit(d, "extern %s%s%s" % (it["name"], self.params(it.get("params", []), it.get("ptypes")),
                                            self.returns(it)))
        elif t == "event":
            ps = it.get("params") or []
            self.emit(d, "event %s%s" % (it["name"], self.params(ps, it.get("ptypes")) if ps else ""))
        elif t == "timer":
            self.emit(d, "timer %s" % it["name"])
        elif t == "const":
            self.emit(d, "const %s = %s" % (it["name"], expr_to_str(it["value"])))
        elif t == "var":
            self.emit(d, self.decl(it))
        elif t == "func":
            self.emit(d, "func %s%s%s:" % (it["name"], self.params(it.get("params", []), it.get("ptypes")),
                                          self.returns(it)))
            self.block(d + 1, it.get("body", []))
        elif t == "on":
            ps = it.get("params")
            self.emit(d, "on %s%s:" % (it["event"], self.params(ps) if ps else ""))
            self.block(d + 1, it.get("body", []))
        elif t == "state":
            self.emit(d, "%sstate %s:" % ("initial " if it.get("initial") else "", it["name"]))
            vs = it.get("vars") or []
            for v in vs:
                self.item(d + 1, v)
            hs = it.get("handlers", [])
            for i, h in enumerate(hs):
                if i or vs:
                    self.blank()
                self.item(d + 1, h)
            if not vs and not any(h["type"] != "comment" for h in hs):
                self.emit(d + 1, "pass")
        else:
            raise ValueError("unknown item type %r" % t)

    def block(self, d, body):
        body = body or []
        for s in body:
            self.stmt(d, s)
        if not any(s["type"] != "comment" for s in body):
            self.emit(d, "pass")

    def stmt(self, d, s):
        t = s["type"]
        if t == "comment":
            self.note(d, {"note": s.get("text", "")})
            self.blank()
            return
        self.note(d, s)
        if t == "assign":
            self.emit(d, "%s = %s" % (s["target"], expr_to_str(s["value"])))
        elif t == "log":
            self.emit(d, "%s %s" % (s["level"], _quote(s["text"])))
        elif t == "massign":
            self.emit(d, "%s = %s" % (", ".join(s["targets"]), expr_to_str(s["value"])))
        elif t == "local":
            self.emit(d, self.decl(s))
        elif t == "call":
            self.emit(d, expr_to_str(s))
        elif t == "if":
            for i, br in enumerate(s["branches"]):
                self.emit(d, ("if " if i == 0 else "elif ") + expr_to_str(br["cond"]) + ":")
                self.block(d + 1, br.get("body", []))
            if s.get("else_body") is not None:
                self.emit(d, "else:")
                self.block(d + 1, s["else_body"])
        elif t == "while":
            self.emit(d, "while " + expr_to_str(s["cond"]) + ":")
            self.block(d + 1, s.get("body", []))
        elif t == "goto":
            self.emit(d, "goto_state " + s["state"])
        elif t == "emit":
            args = s.get("args") or []
            self.emit(d, "raise_event %s%s" % (s["event"], ("(" + ", ".join(expr_to_str(a) for a in args) + ")") if args else ""))
        elif t == "start":
            self.emit(d, "start %s %s%s" % (s["timer"], "every " if s.get("every") else "", expr_to_str(s["ms"])))
        elif t == "stop":
            self.emit(d, "stop " + s["timer"])
        elif t == "return":
            vs = s.get("values") or ([s["value"]] if s.get("value") is not None else [])
            self.emit(d, "return" + ((" " + ", ".join(expr_to_str(v) for v in vs)) if vs else ""))
        else:
            raise ValueError("unknown statement type %r" % t)


def to_source(prog):
    return Printer().program(prog)
