"""Export the visual model as a Mermaid flowchart (for docs, wikis, PRs)."""

from .printer import expr_to_str


def _q(s):
    return '"' + s.replace('"', "#quot;") + '"'


class _M:
    def __init__(self):
        self.lines = ["flowchart TD"]
        self.n = 0

    def node(self, shape, text):
        self.n += 1
        nid = "n%d" % self.n
        l, r = {"proc": ("[", "]"), "io": ("[/", "/]"), "sub": ("[[", "]]"), "dec": ("{", "}"),
                "term": ("([", "])"), "ev": ("{{", "}}"), "goto": ("((", "))"), "emit": (">", "]")}[shape]
        self.lines.append("  %s%s%s%s" % (nid, l, _q(text), r))
        return nid

    def edge(self, a, b, label=None):
        self.lines.append("  %s -->%s %s" % (a, ("|%s|" % label) if label else "", b))

    def block(self, stmts, prev, funcs, label=None):
        """Returns list of (node, label) dangling exits."""
        exits = [(prev, label)]
        for s in stmts:
            if s["type"] == "comment":
                continue
            entry, outs = self.stmt(s, funcs)
            for p, lb in exits:
                self.edge(p, entry, lb)
            exits = outs
        return exits

    def stmt(self, s, funcs):
        t = s["type"]
        if t == "if":
            first = None
            exits = []
            prev_no = None
            for br in s["branches"]:
                d = self.node("dec", expr_to_str(br["cond"]))
                if prev_no:
                    self.edge(prev_no, d, "no")
                first = first or d
                exits += self.block(br["body"], d, funcs, "yes")
                prev_no = d
            if s.get("else_body"):
                exits += self.block(s["else_body"], prev_no, funcs, "no")
            else:
                exits.append((prev_no, "no"))
            return first, exits
        if t == "while":
            d = self.node("dec", "while " + expr_to_str(s["cond"]))
            for p, lb in self.block(s["body"], d, funcs, "yes"):
                self.edge(p, d, lb)
            return d, [(d, "no")]
        if t == "goto":
            return self.node("goto", "goto_state " + s["state"]), []
        if t == "return":
            return self.node("term", "return" + (" " + expr_to_str(s["value"]) if s.get("value") else "")), []
        if t == "assign":
            n = self.node("proc", "%s = %s" % (s["target"], expr_to_str(s["value"])))
        elif t == "local":
            n = self.node("proc", "var %s" % s["name"] + (" = " + expr_to_str(s["value"]) if s.get("value") else ""))
        elif t == "call":
            n = self.node("sub" if s["name"] in funcs else "io", expr_to_str(s))
        elif t == "emit":
            n = self.node("emit", "raise_event " + s["event"])
        elif t == "start":
            n = self.node("proc", "start %s %s%s" % (s["timer"], "every " if s.get("every") else "", expr_to_str(s["ms"])))
        else:
            n = self.node("proc", "stop " + s["timer"])
        return n, [(n, None)]

    def routine(self, r, funcs, title):
        head = self.node("ev" if r["type"] == "on" else "sub", title)
        exits = self.block(r["body"], head, funcs)
        if exits:
            end = self.node("term", "end")
            for p, lb in exits:
                self.edge(p, end, lb)


def to_mermaid(ast):
    m = _M()
    funcs = {i["name"] for i in ast["items"] if i["type"] == "func"}
    for it in ast["items"]:
        if it["type"] == "func":
            m.routine(it, funcs, "func %s(%s)" % (it["name"], ", ".join(it["params"])))
        elif it["type"] == "on":
            m.routine(it, funcs, "on " + it["event"])
        elif it["type"] == "any_state":
            m.lines.append("  subgraph any_state [any_state]")
            for h in it["handlers"]:
                if h["type"] == "on":
                    m.routine(h, funcs, "on " + h["event"])
            m.lines.append("  end")
        elif it["type"] == "state":
            m.lines.append("  subgraph %s [state %s]" % ("s_" + it["name"], it["name"]))
            for h in it["handlers"]:
                if h["type"] == "on":
                    m.routine(h, funcs, "%s: on %s" % (it["name"], h["event"]))
            m.lines.append("  end")
    return "\n".join(m.lines) + "\n"
