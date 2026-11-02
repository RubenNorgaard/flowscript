"""Static SVG flowcharts of a FlowScript program, for hosts that render them in their own pages.

    svg = flowscript.to_svg(source)                       # the flowchart (every statement a node)
    svg = flowscript.to_svg(source, view="states")        # the state diagram
    svg = flowscript.to_svg(source, state="trading")      # highlight the current state

The result is a self-contained ``<svg>`` element: inline it in HTML (e.g. a Django template with
``|safe``) or save it as a .svg file. It carries its own scoped styles, so it needs no stylesheet:

* colours are CSS custom properties (``--fs-bg``, ``--fs-fg``, ``--fs-accent``, ...; see ``THEMES``)
  that the host may override, e.g. ``.fs-chart { --fs-accent: #0a84ff }``;
* ``theme="auto"`` (default) follows ``prefers-color-scheme`` and a ``data-theme="light|dark"``
  attribute on an ancestor (as on ``<html>``); ``"dark"`` and ``"light"`` fix the palette;
* element ids are prefixed per chart, so several charts can share a page.

Every node keeps ``data-ids`` (AST node ids), ``data-line`` (source line) and states keep
``data-state`` (name), so a page can add its own interactivity (highlighting, links to source).

This mirrors the debugger's chart (flowscript/debugger/static/chart.js) without the editing parts.
"""

import hashlib
import math

from .printer import expr_to_str

__all__ = ["to_svg", "THEMES"]

CW = 7.23      # monospace character width at 12px
G = 24         # vertical gap between nodes
HG = 34        # horizontal gap between branches
NH = 34        # node height

# Colour tokens.  "dark" is the debugger's palette; "light" is its daylight counterpart.
THEMES = {
    "dark": {
        "bg": "#15181e", "fg": "#d7dce5", "muted": "#7d8696", "line": "#303643", "accent": "#5aa9ff",
        "cur": "#ffcc4d", "ok": "#4fd18b", "bad": "#ff6b6b", "kw": "#c792ea", "ev": "#ffcb6b",
        "cmt": "#697386", "edge": "#6d7789", "stroke": "#8a94a8", "gate-text": "#e8ecf3",
        "n-proc": "#2b3a55", "n-io": "#2f4a3a", "n-sub": "#3b3357", "n-dec": "#4a3b22",
        "n-goto": "#5a2c3a", "n-emit": "#2a4b55", "n-timer": "#3d4423", "n-ret": "#3a3a3a",
        "n-ev": "#55401f", "n-note": "#2a2d33", "n-end": "#444a55", "n-leaf": "#222833",
        "n-gate": "#2a303c", "decl-bg": "#1b1f27", "state": "#3d5a80", "state-title": "#9ec5ff",
        "state-fill": "rgba(90,169,255,.04)", "sd-fill": "#22324a", "sd-stroke": "#5a7fb0",
        "cur-fill": "rgba(255,204,77,.08)",
    },
    "light": {
        "bg": "#ffffff", "fg": "#1f2430", "muted": "#6b7383", "line": "#d5dae2", "accent": "#1f6fd1",
        "cur": "#c98a00", "ok": "#1f9d5c", "bad": "#d14343", "kw": "#8a3fb8", "ev": "#9a6700",
        "cmt": "#7a8293", "edge": "#7d8696", "stroke": "#7d8696", "gate-text": "#1f2430",
        "n-proc": "#dfe8f7", "n-io": "#dcefe2", "n-sub": "#e9e3f7", "n-dec": "#f6ead2",
        "n-goto": "#f6dde2", "n-emit": "#d9eef2", "n-timer": "#ebefd6", "n-ret": "#e6e6e6",
        "n-ev": "#f7e4c6", "n-note": "#f5f6f8", "n-end": "#d7dbe2", "n-leaf": "#f1f3f6",
        "n-gate": "#e4e8ee", "decl-bg": "#f7f8fa", "state": "#8fb0d8", "state-title": "#2357a0",
        "state-fill": "rgba(31,111,209,.04)", "sd-fill": "#e3edf9", "sd-stroke": "#6f95c8",
        "cur-fill": "rgba(201,138,0,.10)",
    },
}


# ------------------------------------------------------------------------------------- helpers
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _tw(s):
    return len(s) * CW


def _clip(s, n=44):
    return s[:n - 1] + "…" if len(s) > n else s


def _f(v):
    v = round(v * 10) / 10
    return str(int(v)) if v == int(v) else str(v)


def _typed_params(it):
    ptypes = it.get("ptypes") or []
    return ", ".join("%s %s" % (ptypes[i] if i < len(ptypes) else "int", p)
                     for i, p in enumerate(it.get("params") or []))


def _sub_ids(e, acc=None):
    acc = [] if acc is None else acc
    if not isinstance(e, dict):
        return acc
    if "id" in e:
        acc.append(e["id"])
    for k in ("left", "right", "operand", "value", "ms"):
        if isinstance(e.get(k), dict):
            _sub_ids(e[k], acc)
    for a in e.get("args") or []:
        _sub_ids(a, acc)
    return acc


def _is_gate(e):
    return ((e["type"] == "binary" and e["op"] in ("and", "or", "xor")) or
            (e["type"] == "unary" and e["op"] == "not"))


def _gate_inputs(e, chain=None):
    if e["type"] == "unary":
        return [e["operand"]]
    out = []

    def fl(x):
        if x["type"] == "binary" and x["op"] == e["op"]:
            if chain is not None:
                chain.append(x["id"])
            fl(x["left"])
            fl(x["right"])
        else:
            out.append(x)
    fl(e)
    return out


class _Layout:
    """A laid-out piece: extents left/right of its centre line, height, whether it ends the flow."""

    def __init__(self, l, r, h, draw, term=False):
        self.l, self.r, self.h, self.draw, self.term = l, r, h, draw, term


# ------------------------------------------------------------------------------------- renderer
class _Chart:
    def __init__(self, ast, prefix, state=None):
        self.ast = ast
        self.p = prefix
        self.cur_state = state
        self.out = []
        self.funcs = {i["name"] for i in ast["items"] if i["type"] == "func"}

    def P(self, s):
        self.out.append(s)

    def marker(self, name):
        return "url(#%s-%s)" % (self.p, name)

    # --- gates ------------------------------------------------------------------------------
    def gate_tree(self, e, root=None):
        root = e["id"] if root is None else root
        if not _is_gate(e):
            text = _clip(expr_to_str(e), 34)
            w, h = _tw(text) + 18, 22

            def draw(xo, ym):
                self.P('<g class="nd k-leaf" data-ids="%s" data-dec="%s" data-line="%s"><title>%s</title>'
                       '<rect class="sh" x="%s" y="%s" width="%s" height="%s" rx="4"/>'
                       '<text x="%s" y="%s">%s</text></g>'
                       % (" ".join(map(str, _sub_ids(e))), root, e.get("line", 0), _esc(expr_to_str(e)),
                          _f(xo - w), _f(ym - h / 2), _f(w), h, _f(xo - w + 9), _f(ym + 4), _esc(text)))
            return _Layout(w, 0, h, draw)
        chain = []
        inputs = _gate_inputs(e, chain)
        kids = [self.gate_tree(x, root) for x in inputs]
        n = len(kids)
        GW = 42
        GH = 26 if e["type"] == "unary" else max(30, n * 14 + 6)
        stack_h = sum(c.h for c in kids) + 8 * (n - 1)
        cw = max(c.l for c in kids)
        op = "not" if e["type"] == "unary" else e["op"]

        def draw(xo, ym):
            x0, x1, y0, y1 = xo - GW, xo, ym - GH / 2, ym + GH / 2
            y = ym - stack_h / 2
            for k, c in enumerate(kids):
                cy, cx = y + c.h / 2, x0 - 22
                py = ym if n == 1 else y0 + (k + 1) * GH / (n + 1)
                self.P('<path class="wire" d="M%s %s H%s V%s H%s"/>'
                       % (_f(cx), _f(cy), _f(cx + 11), _f(py), _f(x0 + (5 if op in ("or", "xor") else 0))))
                c.draw(cx, cy)
                y += c.h + 8
            if op == "and":
                d = "M%s %s H%s A%s %s 0 0 1 %s %s H%s Z" % (
                    _f(x0), _f(y0), _f(x1 - GH / 2), _f(GH / 2), _f(GH / 2), _f(x1 - GH / 2), _f(y1), _f(x0))
            elif op == "not":
                d = "M%s %s L%s %s L%s %s Z" % (_f(x0), _f(y0), _f(x1 - 9), _f(ym), _f(x0), _f(y1))
            else:
                xs = x0 + 5 if op == "xor" else x0
                d = "M%s %s Q%s %s %s %s Q%s %s %s %s Q%s %s %s %s Z" % (
                    _f(xs), _f(y0), _f(xs + 11), _f(ym), _f(xs), _f(y1), _f(xs + GW * 0.55), _f(y1),
                    _f(x1), _f(ym), _f(xs + GW * 0.55), _f(y0), _f(xs), _f(y0))
            extra = ""
            if op == "xor":
                extra = '<path class="sh" style="fill:none" d="M%s %s Q%s %s %s %s"/>' % (
                    _f(x0), _f(y0), _f(x0 + 11), _f(ym), _f(x0), _f(y1))
            if op == "not":
                extra = '<circle class="sh" cx="%s" cy="%s" r="4.5"/>' % (_f(x1 - 4.5), _f(ym))
            self.P('<g class="nd k-gate" data-ids="%s" data-dec="%s" data-line="%s"><title>%s</title>'
                   '<path class="sh" d="%s"/>%s<text class="gate-op" x="%s" y="%s">%s</text></g>'
                   % (" ".join(map(str, [e["id"]] + chain)), root, e.get("line", 0),
                      _esc(expr_to_str(e)), d, extra, _f(x0 + (4 if op == "not" else 9)), _f(ym + 3.5),
                      op.upper()))
        return _Layout(cw + 22 + GW, 0, max(stack_h, GH), draw)

    # --- shapes -----------------------------------------------------------------------------
    @staticmethod
    def shape(kind, x0, y0, w, h):
        x1, y1, ym = x0 + w, y0 + h, y0 + h / 2
        F = _f
        if kind == "io":
            return '<path class="sh" d="M%s %s H%s L%s %s H%s Z"/>' % (F(x0 + 9), F(y0), F(x1), F(x1 - 9), F(y1), F(x0))
        if kind == "sub":
            return ('<rect class="sh" x="%s" y="%s" width="%s" height="%s"/>'
                    '<path class="sh" d="M%s %s V%s M%s %s V%s" style="fill:none"/>'
                    % (F(x0), F(y0), F(w), F(h), F(x0 + 8), F(y0), F(y1), F(x1 - 8), F(y0), F(y1)))
        if kind == "emit":
            return '<path class="sh" d="M%s %s H%s L%s %s L%s %s H%s Z"/>' % (
                F(x0), F(y0), F(x1 - 13), F(x1), F(ym), F(x1 - 13), F(y1), F(x0))
        if kind in ("goto", "ret"):
            return '<rect class="sh" x="%s" y="%s" width="%s" height="%s" rx="%s"/>' % (F(x0), F(y0), F(w), F(h), F(h / 2))
        if kind == "ev":
            return '<path class="sh" d="M%s %s H%s L%s %s L%s %s H%s L%s %s Z"/>' % (
                F(x0 + 11), F(y0), F(x1 - 11), F(x1), F(ym), F(x1 - 11), F(y1), F(x0 + 11), F(x0), F(ym))
        if kind == "note":
            return '<path class="sh" d="M%s %s H%s L%s %s V%s H%s Z"/>' % (
                F(x0), F(y0), F(x1 - 9), F(x1), F(y0 + 9), F(y1), F(x0))
        if kind == "timer":
            return '<rect class="sh" x="%s" y="%s" width="%s" height="%s" rx="9"/>' % (F(x0), F(y0), F(w), F(h))
        return '<rect class="sh" x="%s" y="%s" width="%s" height="%s" rx="3"/>' % (F(x0), F(y0), F(w), F(h))

    @staticmethod
    def note_layout(note):
        if not note:
            return None
        lines = [_clip(l, 40) for l in str(note).split("\n")]
        return {"w": max(_tw(l) for l in lines) + 16, "h": len(lines) * 14 + 8, "lines": lines}

    def note_block(self, nl, x, y, node_id=None):
        attrs = ' data-ids="%s"' % node_id if node_id is not None else ""
        self.P('<g class="nd k-note"%s>%s%s</g>' % (
            attrs, self.shape("note", x, y, nl["w"], nl["h"]),
            "".join('<text x="%s" y="%s">%s</text>' % (_f(x + 8), _f(y + 16 + i * 14), _esc(l))
                    for i, l in enumerate(nl["lines"]))))

    def draw_note(self, nl, x, y, from_x, from_y):
        self.P('<path class="note-line" d="M%s %s H%s"/>' % (_f(from_x), _f(from_y), _f(x)))
        self.note_block(nl, x, y)

    def node(self, node, kind, text, ids=None, term=False, min_w=96, head=False):
        label = _clip(text, 46)
        pad = 34 if kind == "sub" else 32 if kind in ("io", "emit") else 24
        w = max(min_w, _tw(label) + pad)
        nl = self.note_layout(node.get("note"))
        r = w / 2 + (18 + nl["w"] if nl else 0)
        ids = _sub_ids(node) if ids is None else ids

        def draw(cx, y):
            x0 = cx - w / 2
            self.P('<g class="nd k-%s%s" data-ids="%s" data-line="%s"><title>%s</title>%s'
                   '<text x="%s" y="%s" text-anchor="middle">%s</text></g>'
                   % (kind, " head" if head else "", " ".join(map(str, ids)), node.get("line", 0),
                      _esc(text), self.shape(kind, x0, y, w, NH), _f(cx), _f(y + 21), _esc(label)))
            if nl:
                self.draw_note(nl, cx + w / 2 + 18, y, cx + w / 2, y + NH / 2)
        return _Layout(w / 2, r, max(NH, nl["h"] if nl else 0), draw, term)

    # --- flow ---------------------------------------------------------------------------------
    def arrow(self, x, y0, y1):
        self.P('<path class="edge" d="M%s %s V%s" marker-end="%s"/>' % (_f(x), _f(y0), _f(y1), self.marker("arr")))

    def line(self, x, y0, y1):
        self.P('<path class="edge" d="M%s %s V%s"/>' % (_f(x), _f(y0), _f(y1)))

    def seq(self, stmts):
        items = [self.stmt(s) for s in stmts or []]
        h, l, r = G, 0, 0
        for it in items:
            h += it.h + G
            l, r = max(l, it.l), max(r, it.r)
        term = bool(items) and items[-1].term

        def draw(cx, y):
            yy = y
            for i in range(len(items) + 1):
                prev_term = i > 0 and items[i - 1].term
                if not prev_term:
                    (self.arrow if i < len(items) else self.line)(cx, yy, yy + G)
                yy += G
                if i < len(items):
                    items[i].draw(cx, yy)
                    yy += items[i].h
        return _Layout(l, r, h, draw, term)

    def stmt(self, s):
        t = s["type"]
        E = expr_to_str
        if t == "assign":
            return self.node(s, "proc", "%s = %s" % (s["target"], E(s["value"])))
        if t == "massign":
            v = s["value"]
            return self.node(s, "sub" if v["name"] in self.funcs else "io",
                             "%s = %s" % (", ".join(s["targets"]), E(v)))
        if t == "local":
            return self.node(s, "proc", "%s %s%s" % (s.get("vtype") or "int", s["name"],
                                                     " = " + E(s["value"]) if s.get("value") else ""))
        if t == "call":
            return self.node(s, "sub" if s["name"] in self.funcs else "io", E(s))
        if t == "goto":
            return self.node(s, "goto", "⇒ " + s["state"], term=True, min_w=80)
        if t == "return":
            vs = s.get("values") or ([s["value"]] if s.get("value") else [])
            return self.node(s, "ret", "return" + (" " + ", ".join(E(v) for v in vs) if vs else ""),
                             term=True, min_w=70)
        if t == "emit":
            args = s.get("args") or []
            return self.node(s, "emit", "raise_event %s%s" % (
                s["event"], "(%s)" % ", ".join(E(a) for a in args) if args else ""))
        if t == "start":
            return self.node(s, "timer", "⧗ start %s%s%s" % (s["timer"], " every " if s.get("every") else " ",
                                                                E(s["ms"])))
        if t == "stop":
            return self.node(s, "timer", "⧗ stop " + s["timer"])
        if t == "comment":
            return self.node({"id": s["id"], "line": s.get("line", 0)}, "note",
                             s["text"].split("\n")[0], ids=[s["id"]])
        if t == "if":
            return self.decision(s, 0)
        if t == "while":
            return self.while_(s)
        return self.node(s, "proc", "? " + t)

    def diamond_info(self, cond):
        if _is_gate(cond):
            gt = self.gate_tree(cond)
            dw, dh = 56, 40
            return {"gt": gt, "dw": dw, "dh": dh, "text": "?", "top": max(0, gt.h / 2 - dh / 2 + 4),
                    "gw": gt.l + 14}
        text = _clip(expr_to_str(cond), 30)
        return {"gt": None, "dw": max(80, _tw(text) + 46), "dh": 46, "text": text, "top": 0, "gw": 0}

    def draw_diamond(self, cond, D, cx, y_top, extra_ids):
        y_mid, y_bot = y_top + D["dh"] / 2, y_top + D["dh"]
        ids = ([cond["id"]] if D["gt"] else _sub_ids(cond)) + list(extra_ids)
        hw = D["dw"] / 2
        self.P('<g class="nd k-dec" data-ids="%s" data-line="%s"><title>%s</title>'
               '<path class="sh" d="M%s %s L%s %s L%s %s L%s %s Z"/>'
               '<text x="%s" y="%s" text-anchor="middle">%s</text></g>'
               % (" ".join(map(str, ids)), cond.get("line", 0), _esc(expr_to_str(cond)),
                  _f(cx), _f(y_top), _f(cx + hw), _f(y_mid), _f(cx), _f(y_bot), _f(cx - hw), _f(y_mid),
                  _f(cx), _f(y_mid + 4), _esc(D["text"])))
        if D["gt"]:
            D["gt"].draw(cx - hw - 14, y_mid)
            self.P('<path class="wire" d="M%s %s H%s" marker-end="%s"/>'
                   % (_f(cx - hw - 14), _f(y_mid), _f(cx - hw), self.marker("arr")))
        return y_mid, y_bot

    def yes_no(self, cx, hw, y_mid, y_bot):
        self.P('<text class="lbl-yes" x="%s" y="%s">yes</text>' % (_f(cx + 5), _f(y_bot + 11)))
        self.P('<text class="lbl-no" x="%s" y="%s">no</text>' % (_f(cx + hw + 5), _f(y_mid - 5)))

    def decision(self, s, i):
        br = s["branches"][i]
        D = self.diamond_info(br["cond"])
        Y = self.seq(br["body"])
        if i + 1 < len(s["branches"]):
            inner = self.decision(s, i + 1)

            def ndraw(cx, y, inner=inner):
                self.arrow(cx, y, y + G)
                inner.draw(cx, y + G)
            N = _Layout(inner.l, inner.r, G + inner.h, ndraw, inner.term)
        else:
            N = self.seq(s.get("else_body") or [])
        hw = D["dw"] / 2
        xN = max(hw, Y.r) + HG + N.l
        l = max(Y.l, hw + D["gw"])
        r = xN + max(N.r, 8)
        h = D["top"] + max(D["dh"] + Y.h, D["dh"] / 2 + N.h) + 8
        nl = self.note_layout(s.get("note")) if i == 0 else None

        def draw(cx, y):
            y_top = y + D["top"]
            if D["top"]:
                self.line(cx, y, y_top)
            y_mid, y_bot = self.draw_diamond(br["cond"], D, cx, y_top, [s["id"]] if i == 0 else [])
            merge = y + h
            Y.draw(cx, y_bot)
            if not Y.term:
                self.line(cx, y_bot + Y.h, merge)
            self.P('<path class="edge" d="M%s %s H%s"/>' % (_f(cx + hw), _f(y_mid), _f(cx + xN)))
            N.draw(cx + xN, y_mid)
            if not N.term:
                self.P('<path class="edge" d="M%s %s V%s H%s" marker-end="%s"/>'
                       % (_f(cx + xN), _f(y_mid + N.h), _f(merge), _f(cx + 1), self.marker("arr")))
            self.yes_no(cx, hw, y_mid, y_bot)
            if nl:
                self.draw_note(nl, cx + r + 10, y_top, cx + hw, y_top + 6)
        return _Layout(l, r + (nl["w"] + 20 if nl else 0), h, draw, Y.term and N.term)

    def while_(self, s):
        D = self.diamond_info(s["cond"])
        B = self.seq(s["body"])
        entry = 14
        hw = D["dw"] / 2
        L = max(B.l, hw + D["gw"]) + 16
        R = max(hw, B.r) + 22
        h = entry + D["top"] + D["dh"] + B.h + 18
        nl = self.note_layout(s.get("note"))

        def draw(cx, y):
            y_top = y + entry + D["top"]
            self.arrow(cx, y, y_top)
            y_mid, y_bot = self.draw_diamond(s["cond"], D, cx, y_top, [s["id"]])
            B.draw(cx, y_bot)
            yb = y_bot + B.h
            if not B.term:
                self.P('<path class="edge back" d="M%s %s V%s H%s V%s H%s" marker-end="%s"/>'
                       % (_f(cx), _f(yb), _f(yb + 8), _f(cx - L), _f(y + 7), _f(cx - 2), self.marker("arr")))
            self.P('<path class="edge" d="M%s %s H%s V%s H%s"/>'
                   % (_f(cx + hw), _f(y_mid), _f(cx + R), _f(y + h), _f(cx + 1)))
            self.yes_no(cx, hw, y_mid, y_bot)
            if nl:
                self.draw_note(nl, cx + R + 14, y_top, cx + R, y_top + 6)
        return _Layout(L + 4, R + 4 + (nl["w"] + 20 if nl else 0), h, draw)

    def routine(self, node):
        is_func = node["type"] == "func"
        if is_func:
            head = "func %s(%s)%s" % (node["name"], _typed_params(node),
                                      " -> " + node["returns"] if node.get("returns") else "")
        else:
            head = "on " + node["event"] + ("(%s)" % ", ".join(node["params"]) if node.get("params") else "")
        H = self.node(node, "sub" if is_func else "ev", head, ids=[], min_w=120, head=True)
        B = self.seq(node["body"])
        end_h = 0 if B.term else 22

        def draw(cx, y):
            H.draw(cx, y)
            B.draw(cx, y + H.h)
            if not B.term:
                ye = y + H.h + B.h + 11
                self.P('<g class="nd k-end" data-ids="%s" data-line="%s"><title>end of %s</title>'
                       '<circle class="sh" cx="%s" cy="%s" r="10"/>'
                       '<text class="end" x="%s" y="%s" text-anchor="middle">end</text></g>'
                       % (node["id"], node.get("line", 0), _esc(head), _f(cx), _f(ye), _f(cx), _f(ye + 3)))
        return _Layout(max(H.l, B.l), max(H.r, B.r), H.h + B.h + end_h, draw)

    # --- whole program --------------------------------------------------------------------------
    def decl_lines(self):
        out = []
        for it in self.ast["items"]:
            t = it["type"]
            if t == "extern":
                k, s = "extern", "%s(%s)%s" % (it["name"], _typed_params(it),
                                               " -> " + it["returns"] if it.get("returns") else "")
            elif t == "event":
                k, s = "event", it["name"] + ("(%s)" % _typed_params(it) if it.get("params") else "")
            elif t == "timer":
                k, s = "timer", it["name"]
            elif t == "const":
                k, s = "const", "%s = %s" % (it["name"], expr_to_str(it["value"]))
            elif t == "var":
                k, s = it.get("vtype") or "int", it["name"] + (" = " + expr_to_str(it["value"]) if it.get("value") else "")
            else:
                continue
            out.append((k, _clip(s, 38), it))
        return out

    def card(self, x, y, title, rows):
        """A declarations card: rows of (keyword, text, node)."""
        w = max([_tw(title)] + [_tw(k + " " + t) for k, t, _n in rows]) + 28
        h = len(rows) * 17 + 30
        self.P('<rect class="decl-box" x="%s" y="%s" width="%s" height="%s" rx="6"/>' % (_f(x), _f(y), _f(w), h))
        self.P('<text class="small" x="%s" y="%s">%s</text>' % (_f(x + 10), _f(y + 16), _esc(title)))
        for i, (k, t, n) in enumerate(rows):
            self.P('<g class="nd decl" data-ids="%s" data-line="%s"><text x="%s" y="%s">'
                   '<tspan class="decl-k">%s</tspan> <tspan class="decl-n">%s</tspan></text></g>'
                   % (n["id"], n.get("line", 0), _f(x + 10), _f(y + 35 + i * 17), _esc(k), _esc(t)))
        return w, h

    def render_flow(self):
        M = 24
        y = M
        width = 0
        items = self.ast["items"]
        states = [i for i in items if i["type"] == "state"]
        name = self.ast.get("name")
        title = "flow %s%s" % (name, " v%d" % self.ast["version"] if self.ast.get("version") else "") if name else "program"
        self.P('<text class="sec-title" x="%s" y="%s">%s</text>' % (M, y + 4, _esc(title)))
        y += 18
        x, row_h = M, 0
        decl = self.decl_lines()
        if decl:
            w, h = self.card(x, y, "SYSTEM INTERFACE", decl)
            x += w + 40
            row_h = h
        for nt in (i for i in items if i["type"] == "comment"):
            nl = self.note_layout(nt["text"])
            self.note_block(nl, x, y, nt["id"])
            x += nl["w"] + 30
            row_h = max(row_h, nl["h"])
        for f in (i for i in items if i["type"] == "func"):
            R = self.routine(f)
            R.draw(x + R.l, y)
            x += R.l + R.r + 40
            row_h = max(row_h, R.h)
        width = max(width, x)
        y += row_h + 36

        start = next((s for s in states if s.get("initial")), states[0] if states else None)
        for st in states:
            handlers = [self.routine(h) for h in st["handlers"] if h["type"] == "on"]
            svars = [v for v in st.get("vars") or [] if v["type"] == "var"]
            notes = [h for h in (st.get("vars") or []) + st["handlers"] if h["type"] == "comment"]
            top, pad = y, 18
            sx, sh = M + pad, 0
            cur = self.cur_state == st["name"]
            title_y = top + 22
            box_at = len(self.out)
            self.P("")
            is_start = st is start
            self.P('<text class="state-title" x="%s" y="%s">%sstate %s%s</text>'
                   % (M + pad + (18 if is_start else 0), title_y, "initial " if st.get("initial") else "",
                      _esc(st["name"]), "  \u25c0 current" if cur else ""))
            if is_start:
                self.P('<circle class="initial" cx="%s" cy="%s" r="5"><title>initial state</title></circle>'
                       % (M + pad + 5, title_y - 5))
            iy = top + 36
            if svars:
                rows = [(v.get("vtype") or "int",
                         v["name"] + (" = " + expr_to_str(v["value"]) if v.get("value") else ""), v)
                        for v in svars]
                w, h = self.card(sx, iy, "state variables", rows)
                sx += w + 30
                sh = max(sh, h)
            for nt in notes:
                nl = self.note_layout(nt["text"])
                self.note_block(nl, sx, iy, nt["id"])
                sx += nl["w"] + 30
                sh = max(sh, nl["h"])
            for R in handlers:
                R.draw(sx + R.l, iy)
                sx += R.l + R.r + 36
                sh = max(sh, R.h)
            bw = max(sx - M - 36 + pad + 18, 260)
            bh = sh + 36 + 18
            self.out[box_at] = ('<g class="state%s" data-state="%s"><rect class="state-box" x="%s" y="%s" '
                                'width="%s" height="%s" rx="12"/></g>'
                                % (" cur" if cur else "", _esc(st["name"]), M, top, _f(bw), _f(bh)))
            width = max(width, M + bw)
            y += bh + 26
        return math.ceil(width + M), math.ceil(y + M)

    def render_states(self):
        items = self.ast["items"]
        states = [i for i in items if i["type"] == "state"]
        if not states:
            self.P('<text class="small" x="20" y="40">This program has no states.</text>')
            return 420, 80
        n = len(states)
        RAD = 58
        ring = 0 if n <= 1 else max(150, n * (RAD * 2 + 70) / (2 * math.pi))
        cx0, cy0 = ring + RAD + 150, ring + RAD + 70
        pos = {}
        for i, s in enumerate(states):
            a = -math.pi / 2 + i * 2 * math.pi / n + (math.pi / 2 if n == 2 else 0)
            pos[s["name"]] = (cx0 + ring * math.cos(a), cy0 + ring * math.sin(a))
        edges = {}

        def gotos(stmts, acc):
            for s in stmts or []:
                if s["type"] == "goto":
                    acc.append(s)
                if s["type"] == "if":
                    for b in s["branches"]:
                        gotos(b["body"], acc)
                    gotos(s.get("else_body"), acc)
                if s["type"] == "while":
                    gotos(s["body"], acc)
            return acc

        def add(frm, to, label):
            edges.setdefault((frm, to), []).append(label)

        W, H = math.ceil(cx0 * 2 + 40), math.ceil(cy0 + ring + RAD + 60)
        for st in states:
            for h in st["handlers"]:
                if h["type"] == "on":
                    for g in gotos(h["body"], []):
                        add(st["name"], g["state"], h["event"])
        pt = pos.get
        arr = self.marker("arr2")
        for (frm, to), labels in edges.items():
            a, b = pt(frm), pt(to)
            if not a or not b:
                continue
            label = _esc(_clip(", ".join(dict.fromkeys(labels)), 30))
            if frm == to:
                ax, ay = a
                self.P('<path class="sd-edge" d="M%s %s C%s %s %s %s %s %s" marker-end="%s"/>'
                       % (_f(ax - 20), _f(ay - RAD + 4), _f(ax - 50), _f(ay - RAD - 70), _f(ax + 50),
                          _f(ay - RAD - 70), _f(ax + 20), _f(ay - RAD + 4), arr))
                self.P('<text class="sd-label" x="%s" y="%s" text-anchor="middle">%s</text>'
                       % (_f(ax), _f(ay - RAD - 56), label))
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            d = math.hypot(dx, dy) or 1
            ux, uy = dx / d, dy / d
            nx, ny = -uy, ux
            r0, r1 = RAD, RAD + 2
            sx, sy = a[0] + ux * r0 + nx * 10, a[1] + uy * r0 + ny * 10
            ex, ey = b[0] - ux * r1 + nx * 10, b[1] - uy * r1 + ny * 10
            mx, my = (sx + ex) / 2 + nx * 26, (sy + ey) / 2 + ny * 26
            self.P('<path class="sd-edge" d="M%s %s Q%s %s %s %s" marker-end="%s"/>'
                   % (_f(sx), _f(sy), _f(mx), _f(my), _f(ex), _f(ey), arr))
            self.P('<text class="sd-label" x="%s" y="%s" text-anchor="middle">%s</text>' % (_f(mx), _f(my), label))
        start = next((s for s in states if s.get("initial")), states[0])
        for s in states:
            x, y = pos[s["name"]]
            cur = self.cur_state == s["name"]
            evs = " · ".join(h["event"] for h in s["handlers"] if h["type"] == "on")
            self.P('<g class="nd sd-state%s" data-state="%s" data-line="%s"><circle class="sh" cx="%s" cy="%s" r="%s"/>'
                   '<text class="sd-name" x="%s" y="%s" text-anchor="middle">%s</text>'
                   '<text class="small" x="%s" y="%s" text-anchor="middle">%s</text></g>'
                   % (" cur" if cur else "", _esc(s["name"]), s.get("line", 0), _f(x), _f(y), RAD,
                      _f(x), _f(y - 4), _esc(s["name"]), _f(x), _f(y + 13), _esc(_clip(evs, 18))))
            if s is start:
                self.P('<circle class="initial" cx="%s" cy="%s" r="7"/><path class="sd-edge" d="M%s %s H%s" marker-end="%s"/>'
                       % (_f(x - RAD - 46), _f(y), _f(x - RAD - 39), _f(y), _f(x - RAD - 3), arr))
        return W, H


# ------------------------------------------------------------------------------------- styles
_RULES = """
.fs-chart{background:var(--fs-bg);font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.fs-chart text{font-size:12px;fill:var(--fs-fg)}
.fs-chart .small{font-size:10px;fill:var(--fs-muted)}
.fs-chart .lbl-yes{fill:var(--fs-ok);font-size:10px}.fs-chart .lbl-no{fill:var(--fs-bad);font-size:10px}
.fs-chart .edge{stroke:var(--fs-edge);stroke-width:1.4;fill:none}.fs-chart .edge.back{stroke-dasharray:4 3}
.fs-chart .wire{stroke:var(--fs-edge);stroke-width:1.2;fill:none}
.fs-chart .arrow-head{fill:var(--fs-edge)}
.fs-chart .nd>.sh{stroke:var(--fs-stroke);stroke-width:1.2}
.fs-chart .k-proc>.sh{fill:var(--fs-n-proc)}.fs-chart .k-io>.sh{fill:var(--fs-n-io)}
.fs-chart .k-sub>.sh{fill:var(--fs-n-sub)}.fs-chart .k-dec>.sh{fill:var(--fs-n-dec)}
.fs-chart .k-goto>.sh{fill:var(--fs-n-goto)}.fs-chart .k-emit>.sh{fill:var(--fs-n-emit)}
.fs-chart .k-timer>.sh{fill:var(--fs-n-timer)}.fs-chart .k-ret>.sh{fill:var(--fs-n-ret)}
.fs-chart .k-ev>.sh{fill:var(--fs-n-ev)}.fs-chart .k-end>.sh{fill:var(--fs-n-end)}
.fs-chart .k-leaf>.sh{fill:var(--fs-n-leaf)}.fs-chart .k-gate>.sh{fill:var(--fs-n-gate)}
.fs-chart .k-note>.sh{fill:var(--fs-n-note);stroke-dasharray:3 2}
.fs-chart .k-note text{fill:var(--fs-cmt);font-style:italic}
.fs-chart .note-line{stroke:var(--fs-cmt);stroke-dasharray:2 3;fill:none}
.fs-chart .gate-op{font-size:10px;fill:var(--fs-gate-text)}.fs-chart .end{font-size:10px;fill:var(--fs-muted)}
.fs-chart .sec-title{font-size:13px;font-weight:bold;fill:var(--fs-muted)}
.fs-chart .decl-box{fill:var(--fs-decl-bg);stroke:var(--fs-line)}
.fs-chart .decl-k{fill:var(--fs-kw)}.fs-chart .decl-n{fill:var(--fs-fg)}
.fs-chart .state-box{fill:var(--fs-state-fill);stroke:var(--fs-state);stroke-width:1.5}
.fs-chart .state.cur .state-box{stroke:var(--fs-cur);stroke-width:2.5;fill:var(--fs-cur-fill)}
.fs-chart .state-title{font-size:14px;font-weight:bold;fill:var(--fs-state-title)}
.fs-chart .state.cur+.state-title{fill:var(--fs-cur)}
.fs-chart .initial{fill:var(--fs-fg)}
.fs-chart .sd-state>.sh{fill:var(--fs-sd-fill);stroke:var(--fs-sd-stroke);stroke-width:2}
.fs-chart .sd-state.cur>.sh{fill:var(--fs-cur-fill);stroke:var(--fs-cur);stroke-width:3}
.fs-chart .sd-name{font-size:13px;font-weight:bold}
.fs-chart .sd-edge{stroke:var(--fs-edge);stroke-width:1.5;fill:none}
.fs-chart .sd-label{font-size:11px;fill:var(--fs-ev)}
"""


def _vars(theme):
    return ";".join("--fs-%s:%s" % (k, v) for k, v in THEMES[theme].items())


def _style(theme, cls):
    if theme in THEMES:
        return ".%s{%s}%s" % (cls, _vars(theme), _RULES)
    # auto: dark by default, light when the page prefers it (unless an ancestor says data-theme="dark"),
    # and whatever an ancestor's data-theme says
    return (".%s{%s}" % (cls, _vars("dark")) +
            "@media (prefers-color-scheme:light){:not([data-theme=\"dark\"])>.%s,.%s{%s}"
            "[data-theme=\"dark\"] .%s{%s}}" % (cls, cls, _vars("light"), cls, _vars("dark")) +
            "[data-theme=\"light\"] .%s{%s}[data-theme=\"dark\"] .%s{%s}"
            % (cls, _vars("light"), cls, _vars("dark")) + _RULES)


# ------------------------------------------------------------------------------------- entry point
def to_svg(program, view="flow", *, state=None, theme="auto", width=None, id_prefix=None, title=None):
    """An SVG flowchart (``view="flow"``) or state diagram (``view="states"``) as a string.

    program:   FlowScript source text, a parsed AST (dict) or a ``Program`` from ``flowscript.compile``
               (it keeps its source).  A program loaded from bytecode cannot be drawn: the chart is
               built from the source, which bytecode does not contain.
    state:     name of the current state, highlighted (e.g. a runner's live state).
    theme:     "auto" (follow the page), "dark" or "light".
    width:     None: the natural size in pixels; "100%" (or any CSS width): scale to the container.
    id_prefix: prefix for element ids (default: derived from the chart, unique per program/view/state).
    title:     accessible title (default: the program's name).
    """
    ast = _ast_of(program)
    if view not in ("flow", "states"):
        raise ValueError("view must be 'flow' or 'states'")
    if theme not in ("auto", "dark", "light"):
        raise ValueError("theme must be 'auto', 'dark' or 'light'")
    if id_prefix is None:
        key = repr((_strip(ast), view, state)).encode("utf-8")
        id_prefix = "fs" + hashlib.sha1(key).hexdigest()[:8]
    chart = _Chart(ast, id_prefix, state)
    W, H = chart.render_flow() if view == "flow" else chart.render_states()
    cls = "fs-chart"
    label = title or ("flow " + ast["name"] if ast.get("name") else "FlowScript program")
    size = ('width="%s" height="%s"' % (W, H) if width is None else
            'width="%s" style="max-width:%dpx;height:auto"' % (_esc(width), W))
    defs = ('<defs><marker id="{p}-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path class="arrow-head" d="M0 1 L10 5 L0 9 z"/></marker>'
            '<marker id="{p}-arr2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
            'orient="auto"><path class="arrow-head" d="M0 1 L10 5 L0 9 z"/></marker></defs>').format(p=id_prefix)
    return ('<svg xmlns="http://www.w3.org/2000/svg" class="%s" %s viewBox="0 0 %d %d" role="img" '
            'aria-label="%s" data-view="%s"><title>%s</title><style>%s</style>%s%s</svg>'
            % (cls, size, W, H, _esc(label), view, _esc(label), _style(theme, cls), defs, "".join(chart.out)))


def _strip(ast):
    """The AST without positions, for a stable id prefix."""
    if isinstance(ast, dict):
        return tuple(sorted((k, _strip(v)) for k, v in ast.items() if k not in ("id", "line")))
    if isinstance(ast, list):
        return tuple(_strip(v) for v in ast)
    return ast


def _ast_of(program):
    from .parser import parse
    if isinstance(program, str):
        return parse(program)
    if isinstance(program, dict) and program.get("type") == "program":
        return program
    ast = getattr(program, "ast", None)
    if ast is not None:
        return ast
    raise TypeError("to_svg needs FlowScript source, an AST or a Program from flowscript.compile() "
                    "(bytecode alone has no source to draw)")
