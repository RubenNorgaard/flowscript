/* FlowScript flowchart renderer.
 *
 * The chart is a pure function of the AST (visual model). Every statement becomes one
 * node; conditions are drawn as logic-gate trees feeding the decision diamonds.
 * Elements carry:  data-ids="<node ids>"  (for execution highlighting)
 *                  data-sel="<node id>"   (selection target for editing)
 *                  data-line="<line>"     (breakpoints / source sync)
 */
"use strict";

const FC = (function () {
  const CW = 7.23;      // monospace char width @12px
  const G = 24;         // vertical gap between nodes
  const HG = 34;        // horizontal gap between branches
  const NH = 34;        // node height
  let OPTS = {};
  let OUT = [];
  let BLOCKS = [];

  // ---------------------------------------------------------------- utilities
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const tw = (s) => s.length * CW;
  const clip = (s, n) => (s.length > (n || 44) ? s.slice(0, (n || 44) - 1) + "…" : s);
  const f1 = (v) => Math.round(v * 10) / 10;
  const P = (s) => OUT.push(s);

  // ---------------------------------------------------------------- expression printer (mirrors printer.py)
  const LEVELS = [["or"], ["xor"], ["and"], null, ["==", "!=", "<", "<=", ">", ">="], ["|"], ["^"], ["&"], ["<<", ">>"], ["+", "-"], ["*", "/", "%"]];
  const LVL = {};
  LEVELS.forEach((ops, i) => ops && ops.forEach((o) => (LVL[o] = i)));
  const NOTL = 3, UNL = LEVELS.length, ATOM = 100;
  const CMP = new Set(LEVELS[4]);
  function prec(e) {
    if (e.type === "binary") return LVL[e.op];
    if (e.type === "unary") return e.op === "not" ? NOTL : UNL;
    if (e.type === "num" && e.value < 0) return UNL;
    return ATOM;
  }
  // "int a, float b"
  function typedParams(it) {
    return (it.params || []).map((p, i) => ((it.ptypes || [])[i] || "int") + " " + p).join(", ");
  }

  function exprStr(e) {
    if (!e) return "";
    switch (e.type) {
      case "num": return e.text || String(e.value);
      case "bool": return e.value ? "true" : "false";
      case "str": return JSON.stringify(e.value);
      case "name": return e.name;
      case "call": case "callx": return e.name + "(" + (e.args || []).map(exprStr).join(", ") + ")";
      case "unary": {
        let s = exprStr(e.operand);
        if (prec(e.operand) < (e.op === "not" ? NOTL : UNL)) s = "(" + s + ")";
        return e.op === "not" ? "not " + s : e.op + s;
      }
      case "binary": {
        const l = LVL[e.op];
        let ls = exprStr(e.left), rs = exprStr(e.right);
        if (prec(e.left) < (CMP.has(e.op) ? l + 1 : l)) ls = "(" + ls + ")";
        if (prec(e.right) <= l) rs = "(" + rs + ")";
        return ls + " " + e.op + " " + rs;
      }
    }
    return "?";
  }

  function subIds(e, acc) {
    acc = acc || [];
    if (!e || typeof e !== "object") return acc;
    if (e.id !== undefined) acc.push(e.id);
    for (const k of ["left", "right", "operand", "value", "ms"]) if (e[k] && typeof e[k] === "object") subIds(e[k], acc);
    for (const a of e.args || []) subIds(a, acc);
    return acc;
  }

  // ---------------------------------------------------------------- gates
  const isGate = (e) => (e.type === "binary" && (e.op === "and" || e.op === "or" || e.op === "xor")) || (e.type === "unary" && e.op === "not");
  function gateInputs(e, chain) {
    if (e.type === "unary") return [e.operand];
    const out = [];
    (function fl(x) {
      if (x.type === "binary" && x.op === e.op) { if (chain) chain.push(x.id); fl(x.left); fl(x.right); } else out.push(x);
    })(e);
    return out;
  }
  function truth(e) {
    const vals = OPTS.values || {};
    if (vals[e.id] !== undefined) return vals[e.id] ? 1 : 0;
    if (!isGate(e)) return null;
    const ins = gateInputs(e).map(truth);
    if (e.type === "unary") return ins[0] === null ? null : 1 - ins[0];
    if (e.op === "and") return ins.includes(0) ? 0 : ins.every((x) => x === 1) ? 1 : null;
    if (e.op === "or") return ins.includes(1) ? 1 : ins.every((x) => x === 0) ? 0 : null;
    return ins.includes(null) ? null : ins.reduce((a, b) => a ^ b, 0);
  }
  const tcls = (t) => (t === null ? "" : t ? " t" : " f");

  function gateTree(e, root) {
    if (root === undefined) root = e.id;
    if (!isGate(e)) {
      const text = clip(exprStr(e), 34);
      const w = tw(text) + 18, h = 22;
      return {
        w, h, draw(xo, ym) {
          const raw = (OPTS.values || {})[e.id];
          P(`<g class="nd k-leaf${tcls(truth(e))}" data-ids="${subIds(e).join(" ")}" data-sel="${e.id}" data-dec="${root}" data-line="${e.line}">` +
            `<title>${esc(exprStr(e))}</title><rect class="sh" x="${f1(xo - w)}" y="${f1(ym - h / 2)}" width="${f1(w)}" height="${h}" rx="4"/>` +
            `<text x="${f1(xo - w + 9)}" y="${f1(ym + 4)}">${esc(text)}</text>` +
            (raw !== undefined ? `<text class="val" x="${f1(xo - w + 3)}" y="${f1(ym - h / 2 - 3)}">= ${raw}</text>` : "") + `</g>`);
        },
      };
    }
    const chain = [];
    const inputs = gateInputs(e, chain);
    const kids = inputs.map((x) => gateTree(x, root));
    const n = kids.length;
    const GW = 42, GH = e.type === "unary" ? 26 : Math.max(30, n * 14 + 6);
    const stackH = kids.reduce((s, c) => s + c.h, 0) + 8 * (n - 1);
    const cw = Math.max(...kids.map((c) => c.w));
    return {
      w: cw + 22 + GW, h: Math.max(stackH, GH),
      draw(xo, ym) {
        const x0 = xo - GW, x1 = xo, y0 = ym - GH / 2, y1 = ym + GH / 2;
        let y = ym - stackH / 2;
        kids.forEach((c, k) => {
          const cy = y + c.h / 2, cx = x0 - 22;
          const py = n === 1 ? ym : y0 + ((k + 1) * GH) / (n + 1);
          P(`<path class="wire${tcls(truth(inputs[k]))}" d="M${f1(cx)} ${f1(cy)} H${f1(cx + 11)} V${f1(py)} H${f1(x0 + (e.op === "or" || e.op === "xor" ? 5 : 0))}"/>`);
          c.draw(cx, cy);
          y += c.h + 8;
        });
        const op = e.type === "unary" ? "not" : e.op;
        let d;
        if (op === "and") d = `M${x0} ${f1(y0)} H${f1(x1 - GH / 2)} A${GH / 2} ${GH / 2} 0 0 1 ${f1(x1 - GH / 2)} ${f1(y1)} H${x0} Z`;
        else if (op === "not") d = `M${x0} ${f1(y0)} L${x1 - 9} ${f1(ym)} L${x0} ${f1(y1)} Z`;
        else {
          const xs = op === "xor" ? x0 + 5 : x0;
          d = `M${xs} ${f1(y0)} Q${xs + 11} ${f1(ym)} ${xs} ${f1(y1)} Q${f1(xs + GW * 0.55)} ${f1(y1)} ${x1} ${f1(ym)} Q${f1(xs + GW * 0.55)} ${f1(y0)} ${xs} ${f1(y0)} Z`;
        }
        P(`<g class="nd k-gate${tcls(truth(e))}" data-ids="${[e.id].concat(chain).join(" ")}" data-sel="${e.id}" data-dec="${root}" data-line="${e.line}"><title>${esc(exprStr(e))}</title><path class="sh" d="${d}"/>` +
          (op === "xor" ? `<path class="sh" style="fill:none" d="M${x0} ${f1(y0)} Q${x0 + 11} ${f1(ym)} ${x0} ${f1(y1)}"/>` : "") +
          (op === "not" ? `<circle class="sh" cx="${x1 - 4.5}" cy="${f1(ym)}" r="4.5"/>` : "") +
          `<text class="small" x="${f1(x0 + (op === "not" ? 4 : 9))}" y="${f1(ym + 3.5)}" style="fill:#e8ecf3">${op.toUpperCase()}</text></g>`);
      },
    };
  }

  // ---------------------------------------------------------------- node shapes
  function shapePath(kind, x0, y0, w, h) {
    const x1 = x0 + w, y1 = y0 + h, ym = y0 + h / 2;
    switch (kind) {
      case "io": return `<path class="sh" d="M${f1(x0 + 9)} ${y0} H${f1(x1)} L${f1(x1 - 9)} ${y1} H${f1(x0)} Z"/>`;
      case "sub": return `<rect class="sh" x="${f1(x0)}" y="${y0}" width="${f1(w)}" height="${h}"/>` +
        `<path class="sh" d="M${f1(x0 + 8)} ${y0} V${y1} M${f1(x1 - 8)} ${y0} V${y1}" style="fill:none"/>`;
      case "emit": return `<path class="sh" d="M${f1(x0)} ${y0} H${f1(x1 - 13)} L${f1(x1)} ${f1(ym)} L${f1(x1 - 13)} ${y1} H${f1(x0)} Z"/>`;
      case "goto": case "ret": return `<rect class="sh" x="${f1(x0)}" y="${y0}" width="${f1(w)}" height="${h}" rx="${h / 2}"/>`;
      case "ev": return `<path class="sh" d="M${f1(x0 + 11)} ${y0} H${f1(x1 - 11)} L${f1(x1)} ${f1(ym)} L${f1(x1 - 11)} ${y1} H${f1(x0 + 11)} L${f1(x0)} ${f1(ym)} Z"/>`;
      case "note": return `<path class="sh" d="M${f1(x0)} ${y0} H${f1(x1 - 9)} L${f1(x1)} ${y0 + 9} V${y1} H${f1(x0)} Z"/>`;
      case "timer": return `<rect class="sh" x="${f1(x0)}" y="${y0}" width="${f1(w)}" height="${h}" rx="9"/>`;
      default: return `<rect class="sh" x="${f1(x0)}" y="${y0}" width="${f1(w)}" height="${h}" rx="3"/>`;
    }
  }

  function noteLayout(node) {
    if (!node.note) return null;
    const lines = String(node.note).split("\n").map((l) => clip(l, 40));
    const w = Math.max(...lines.map(tw)) + 16, h = lines.length * 14 + 8;
    return { w, h, lines };
  }

  function drawNote(nl, x, y, fromX, fromY) {
    P(`<path class="note-line" d="M${f1(fromX)} ${f1(fromY)} H${f1(x)}"/>`);
    P(`<g class="k-note nd" data-sel-note="1">${shapePath("note", x, y, nl.w, nl.h)}` +
      nl.lines.map((l, i) => `<text x="${f1(x + 8)}" y="${y + 16 + i * 14}">${esc(l)}</text>`).join("") + `</g>`);
  }

  function bpCls(node) { return OPTS.breakLines && OPTS.breakLines.has(node.line) ? " bp" : ""; }

  // simple statement node
  function nodeLayout(node, kind, text, opts) {
    opts = opts || {};
    const label = clip(text, 46);
    const pad = kind === "sub" ? 34 : kind === "io" || kind === "emit" ? 32 : 24;
    const w = Math.max(opts.minW || 96, tw(label) + pad);
    const h = NH;
    const nl = noteLayout(node);
    const r = w / 2 + (nl ? 18 + nl.w : 0);
    return {
      l: w / 2, r, h: Math.max(h, nl ? nl.h : 0), term: !!opts.term,
      draw(cx, y) {
        const x0 = cx - w / 2;
        const ids = opts.ids || subIds(node);
        P(`<g class="nd k-${kind}${bpCls(node)}" data-ids="${ids.join(" ")}" data-sel="${node.id}" data-line="${node.line}"` +
          (opts.head ? ` data-head="${opts.head}"` : "") + `><title>${esc(text)}</title>${shapePath(kind, x0, y, w, h)}` +
          `<text x="${f1(cx)}" y="${y + 21}" text-anchor="middle">${esc(label)}</text>` +
          (bpCls(node) ? `<circle class="bpdot" cx="${f1(x0 - 7)}" cy="${y + h / 2}" r="4"/>` : "") + `</g>`);
        if (nl) drawNote(nl, cx + w / 2 + 18, y, cx + w / 2, y + h / 2);
      },
    };
  }

  // ---------------------------------------------------------------- edges / handles
  const arrow = (x, y0, y1) => P(`<path class="edge" d="M${f1(x)} ${f1(y0)} V${f1(y1)}" marker-end="url(#arr)"/>`);
  const line = (x, y0, y1) => P(`<path class="edge" d="M${f1(x)} ${f1(y0)} V${f1(y1)}"/>`);
  function plus(x, y, bid, idx) {
    P(`<g class="plus" data-block="${bid}" data-index="${idx}"><circle cx="${f1(x)}" cy="${f1(y)}" r="7"/><text x="${f1(x)}" y="${f1(y + 4)}" style="font-size:11px">+</text></g>`);
  }
  function registerBlock(ref) { BLOCKS.push(ref); return BLOCKS.length - 1; }

  // ---------------------------------------------------------------- statements
  function seq(stmts, ref) {
    stmts = stmts || [];
    const items = stmts.map(stmtLayout);
    const editing = OPTS.edit && ref;
    let h = G, l = editing ? 10 : 0, r = editing ? 10 : 0;
    for (const it of items) { h += it.h + G; l = Math.max(l, it.l); r = Math.max(r, it.r); }
    const term = items.length > 0 && items[items.length - 1].term;
    const bid = editing ? registerBlock(ref) : null;
    return {
      l, r, h, term,
      draw(cx, y) {
        let yy = y;
        for (let i = 0; i <= items.length; i++) {
          const prevTerm = i > 0 && items[i - 1].term;
          if (!prevTerm) (i < items.length ? arrow : line)(cx, yy, yy + G);
          if (editing) plus(cx, yy + G / 2, bid, i);
          yy += G;
          if (i < items.length) { items[i].draw(cx, yy); yy += items[i].h; }
        }
      },
    };
  }

  function stmtLayout(s) {
    switch (s.type) {
      case "assign": return nodeLayout(s, "proc", s.target + " = " + exprStr(s.value));
      case "massign": {
        const isFunc = (OPTS.funcs || new Set()).has(s.value.name);
        return nodeLayout(s, isFunc ? "sub" : "io", s.targets.join(", ") + " = " + exprStr(s.value));
      }
      case "local": return nodeLayout(s, "proc", (s.vtype || "int") + " " + s.name + (s.value ? " = " + exprStr(s.value) : ""));
      case "call": {
        const isFunc = (OPTS.funcs || new Set()).has(s.name);
        return nodeLayout(s, isFunc ? "sub" : "io", exprStr(s));
      }
      case "goto": return nodeLayout(s, "goto", "⇒ " + s.state, { term: true, minW: 80 });
      case "return": {
        const vs = s.values || (s.value ? [s.value] : []);
        return nodeLayout(s, "ret", "return" + (vs.length ? " " + vs.map(exprStr).join(", ") : ""), { term: true, minW: 70 });
      }
      case "emit": return nodeLayout(s, "emit", "raise_event " + s.event + ((s.args || []).length ? "(" + s.args.map(exprStr).join(", ") + ")" : ""));
      case "start": return nodeLayout(s, "timer", "⧗ start " + s.timer + (s.every ? " every " : " ") + exprStr(s.ms));
      case "stop": return nodeLayout(s, "timer", "⧗ stop " + s.timer);
      case "comment": return nodeLayout({ id: s.id, line: s.line }, "note", s.text.split("\n")[0], { ids: [s.id] });
      case "if": return decision(s, 0);
      case "while": return whileLayout(s);
    }
    return nodeLayout(s, "proc", "? " + s.type);
  }

  function diamondInfo(cond) {
    if (isGate(cond)) {
      const gt = gateTree(cond);
      const dw = 56, dh = 40;
      return { gt, dw, dh, text: "?", topPad: Math.max(0, gt.h / 2 - dh / 2 + 4), gw: gt.w + 14 };
    }
    const text = clip(exprStr(cond), 30);
    const dw = Math.max(80, tw(text) + 46), dh = 46;
    return { gt: null, dw, dh, text, topPad: 0, gw: 0 };
  }

  function drawDiamond(s, cond, D, cx, yTop, extraIds, branch) {
    const yMid = yTop + D.dh / 2, yBot = yTop + D.dh;
    const ids = (D.gt ? [cond.id] : subIds(cond)).concat(extraIds || []);
    const t = truth(cond);
    const showT = D.gt ? tcls(t) : "";
    P(`<g class="nd k-dec${showT}${bpCls(branch === 0 ? s : cond)}" data-ids="${ids.join(" ")}" data-sel="${s.id}" data-branch="${branch}" data-line="${cond.line}">` +
      `<title>${esc(exprStr(cond))}</title><path class="sh" d="M${f1(cx)} ${f1(yTop)} L${f1(cx + D.dw / 2)} ${f1(yMid)} L${f1(cx)} ${f1(yBot)} L${f1(cx - D.dw / 2)} ${f1(yMid)} Z"/>` +
      `<text x="${f1(cx)}" y="${f1(yMid + 4)}" text-anchor="middle">${esc(D.text)}</text>` +
      (!D.gt && (OPTS.values || {})[cond.id] !== undefined ? `<text class="val" x="${f1(cx + D.dw / 4 + 4)}" y="${f1(yTop + 8)}">= ${(OPTS.values || {})[cond.id]}</text>` : "") + `</g>`);
    if (D.gt) {
      D.gt.draw(cx - D.dw / 2 - 14, yMid);
      P(`<path class="wire${tcls(t)}" d="M${f1(cx - D.dw / 2 - 14)} ${f1(yMid)} H${f1(cx - D.dw / 2)}" marker-end="url(#arr)"/>`);
    }
    return { yMid, yBot };
  }

  function decision(s, i) {
    const br = s.branches[i];
    const D = diamondInfo(br.cond);
    const Y = seq(br.body, { node: s, key: "branch", bi: i });
    let N;
    if (i + 1 < s.branches.length) {
      const inner = decision(s, i + 1);
      N = { l: inner.l, r: inner.r, h: G + inner.h, term: inner.term, draw(cx, y) { arrow(cx, y, y + G); inner.draw(cx, y + G); } };
    } else {
      N = seq(s.else_body || [], { node: s, key: "else_body" });
    }
    const xN = Math.max(D.dw / 2, Y.r) + HG + N.l;
    const l = Math.max(Y.l, D.dw / 2 + D.gw);
    const r = xN + Math.max(N.r, 8);
    const h = D.topPad + Math.max(D.dh + Y.h, D.dh / 2 + N.h) + 8;
    const nl = noteLayout(s);
    return {
      l, r: r + (nl && i === 0 ? nl.w + 20 : 0), h, term: Y.term && N.term,
      draw(cx, y) {
        const yTop = y + D.topPad;
        if (D.topPad) line(cx, y, yTop);
        const { yMid, yBot } = drawDiamond(s, br.cond, D, cx, yTop, i === 0 ? [s.id] : [], i);
        const mergeY = y + h;
        Y.draw(cx, yBot);
        if (!Y.term) line(cx, yBot + Y.h, mergeY);
        P(`<path class="edge" d="M${f1(cx + D.dw / 2)} ${f1(yMid)} H${f1(cx + xN)}"/>`);
        N.draw(cx + xN, yMid);
        if (!N.term) P(`<path class="edge" d="M${f1(cx + xN)} ${f1(yMid + N.h)} V${f1(mergeY)} H${f1(cx + 1)}" marker-end="url(#arr)"/>`);
        P(`<text class="lbl-yes" x="${f1(cx + 5)}" y="${f1(yBot + 11)}">yes</text>`);
        P(`<text class="lbl-no" x="${f1(cx + D.dw / 2 + 5)}" y="${f1(yMid - 5)}">no</text>`);
        if (nl && i === 0) drawNote(nl, cx + r + 10, yTop, cx + D.dw / 2, yTop + 6);
      },
    };
  }

  function whileLayout(s) {
    const D = diamondInfo(s.cond);
    const B = seq(s.body, { node: s, key: "body" });
    const entry = 14;
    const L = Math.max(B.l, D.dw / 2 + D.gw) + 16;
    const R = Math.max(D.dw / 2, B.r) + 22;
    const h = entry + D.topPad + D.dh + B.h + 18;
    const nl = noteLayout(s);
    return {
      l: L + 4, r: R + 4 + (nl ? nl.w + 20 : 0), h, term: false,
      draw(cx, y) {
        const yTop = y + entry + D.topPad;
        arrow(cx, y, yTop);
        const { yMid, yBot } = drawDiamond(s, s.cond, D, cx, yTop, [s.id], 0);
        B.draw(cx, yBot);
        const yb = yBot + B.h;
        if (!B.term) P(`<path class="edge back" d="M${f1(cx)} ${f1(yb)} V${f1(yb + 8)} H${f1(cx - L)} V${f1(y + 7)} H${f1(cx - 2)}" marker-end="url(#arr)"/>`);
        P(`<path class="edge" d="M${f1(cx + D.dw / 2)} ${f1(yMid)} H${f1(cx + R)} V${f1(y + h)} H${f1(cx + 1)}"/>`);
        P(`<text class="lbl-yes" x="${f1(cx + 5)}" y="${f1(yBot + 11)}">yes</text>`);
        P(`<text class="lbl-no" x="${f1(cx + D.dw / 2 + 5)}" y="${f1(yMid - 5)}">no</text>`);
        if (nl) drawNote(nl, cx + R + 14, yTop, cx + R, yTop + 6);
      },
    };
  }

  function routineLayout(node, stateName) {
    const isFunc = node.type === "func";
    const head = isFunc
      ? "func " + node.name + "(" + typedParams(node) + ")" + (node.returns ? " -> " + node.returns : "")
      : "on " + node.event + (node.params ? "(" + node.params.join(", ") + ")" : "");
    const H = nodeLayout(node, isFunc ? "sub" : "ev", head, { ids: [], head: node.id, minW: 120 });
    const B = seq(node.body, { node, key: "body" });
    const endH = B.term ? 0 : 22;
    return {
      l: Math.max(H.l, B.l), r: Math.max(H.r, B.r), h: H.h + B.h + endH, node,
      draw(cx, y) {
        H.draw(cx, y);
        B.draw(cx, y + H.h);
        if (!B.term) {
          const ye = y + H.h + B.h + 11;
          P(`<g class="nd k-end" data-ids="${node.id}" data-sel="${node.id}" data-line="${node.line}"><title>end of ${esc(head)}</title><circle class="sh" cx="${f1(cx)}" cy="${f1(ye)}" r="10"/><text class="small" x="${f1(cx)}" y="${f1(ye + 3)}" text-anchor="middle" style="fill:#ccc">end</text></g>`);
        }
      },
    };
  }

  // ---------------------------------------------------------------- whole program
  function declLines(ast) {
    const out = [];
    for (const it of ast.items) {
      let k = null, t = null;
      switch (it.type) {
        case "extern": k = "extern"; t = it.name + "(" + typedParams(it) + ")" + (it.returns ? " -> " + it.returns : ""); break;
        case "event": k = "event"; t = it.name + (it.params.length ? "(" + typedParams(it) + ")" : ""); break;
        case "timer": k = "timer"; t = it.name; break;
        case "const": k = "const"; t = it.name + " = " + exprStr(it.value); break;
        case "var": k = it.vtype || "int"; t = it.name + (it.value ? " = " + exprStr(it.value) : ""); break;
      }
      if (k) out.push({ k, t: clip(t, 38), node: it });
    }
    return out;
  }

  function addButton(x, y, label, action, arg) {
    const w = tw(label) + 16;
    P(`<g class="addbtn" data-add="${action}" data-arg="${esc(arg || "")}"><rect x="${f1(x)}" y="${f1(y)}" width="${f1(w)}" height="22" rx="5"/><text x="${f1(x + 8)}" y="${f1(y + 15)}">${esc(label)}</text></g>`);
    return w;
  }

  function render(ast, opts) {
    OPTS = opts || {};
    OUT = [];
    BLOCKS = [];
    const M = 24;
    let y = M;
    let width = 0;
    const funcs = new Set(ast.items.filter((i) => i.type === "func").map((i) => i.name));
    OPTS.funcs = funcs;
    const states = ast.items.filter((i) => i.type === "state");

    // --- global row: declarations card + funcs + top-level notes
    const decl = declLines(ast);
    const globalRoutines = ast.items.filter((i) => i.type === "func").map((n) => routineLayout(n, null));
    const notes = ast.items.filter((i) => i.type === "comment");
    P(`<text class="sec-title" x="${M}" y="${y + 4}">${esc(ast.name ? "flow " + ast.name + (ast.version ? " v" + ast.version : "") : "program")}</text>`);
    y += 18;
    let x = M;
    let rowH = 0;
    if (decl.length) {
      const w = Math.max(...decl.map((d) => tw(d.k + " " + d.t))) + 28;
      const h = decl.length * 17 + 30;
      P(`<rect class="decl-box" x="${x}" y="${y}" width="${f1(w)}" height="${h}" rx="6"/>`);
      P(`<text class="small" x="${x + 10}" y="${y + 16}">SYSTEM INTERFACE</text>`);
      decl.forEach((d, i) => {
        P(`<g class="nd" data-sel="${d.node.id}" data-ids="${d.node.id}" data-line="${d.node.line}"><rect class="sh" style="fill:transparent;stroke:none" x="${x + 4}" y="${y + 22 + i * 17}" width="${f1(w - 8)}" height="17"/>` +
          `<text x="${x + 10}" y="${y + 35 + i * 17}"><tspan class="decl-k">${d.k}</tspan> <tspan class="decl-n">${esc(d.t)}</tspan></text></g>`);
      });
      x += w + 40;
      rowH = h;
    }
    for (const nt of notes) {
      const nl = noteLayout({ note: nt.text });
      P(`<g class="nd k-note" data-sel="${nt.id}" data-ids="${nt.id}">${shapePath("note", x, y, nl.w, nl.h)}` +
        nl.lines.map((l, i) => `<text x="${f1(x + 8)}" y="${y + 16 + i * 14}">${esc(l)}</text>`).join("") + `</g>`);
      x += nl.w + 30;
      rowH = Math.max(rowH, nl.h);
    }
    for (const R of globalRoutines) {
      R.draw(x + R.l, y);
      x += R.l + R.r + 40;
      rowH = Math.max(rowH, R.h);
    }
    if (OPTS.edit) {
      let bx = x, by = y;
      for (const [label, act] of [["+ func", "func"], ["+ state", "state"], ["+ declaration", "decl"]]) {
        addButton(bx, by, label, act, "");
        by += 28;
      }
      x += 120;
      rowH = Math.max(rowH, 3 * 28);
    }
    width = Math.max(width, x);
    y += rowH + 36;

    // --- the states
    const stateBoxes = {};
    const startState = states.find((s) => s.initial) || states[0];
    states.forEach((st) => {
      const handlers = st.handlers.filter((h) => h.type === "on").map((h) => routineLayout(h, st.name));
      const svars = (st.vars || []).filter((v) => v.type === "var");
      const hnotes = (st.vars || []).concat(st.handlers).filter((h) => h.type === "comment");
      const top = y, pad = 18;
      let sx = M + pad;
      let sh = 0;
      const cur = OPTS.curState === st.name;
      const titleY = top + 22;
      const boxStart = OUT.length;
      P(""); // placeholder for container rect
      const start = st === startState;
      P(`<text class="state-title" x="${M + pad + (start ? 18 : 0)}" y="${titleY}">${st.initial ? "initial " : ""}state ${esc(st.name)}${cur ? "  ◀ current" : ""}</text>`);
      if (start) P(`<circle class="initial" cx="${M + pad + 5}" cy="${titleY - 5}" r="5"><title>initial state</title></circle>`);
      const iy = top + 36;
      if (svars.length) {
        // state variables: a small card, reset to these values on every entry
        const lines = svars.map((v) => v.name + (v.value ? " = " + exprStr(v.value) : ""));
        const w = Math.max(tw("state variables"), ...lines.map((l, i) => tw((svars[i].vtype || "int") + " " + l))) + 28;
        const h = svars.length * 17 + 30;
        P(`<rect class="decl-box" x="${f1(sx)}" y="${iy}" width="${f1(w)}" height="${h}" rx="6"><title>reset to these values each time the state is entered</title></rect>`);
        P(`<text class="small" x="${f1(sx + 10)}" y="${iy + 15}">state variables</text>`);
        svars.forEach((v, i) => {
          P(`<g class="nd" data-sel="${v.id}" data-ids="${v.id}" data-line="${v.line}"><rect class="sh" style="fill:transparent;stroke:none" x="${f1(sx + 4)}" y="${iy + 22 + i * 17}" width="${f1(w - 8)}" height="17"/>` +
            `<text x="${f1(sx + 10)}" y="${iy + 35 + i * 17}"><tspan class="decl-k">${esc(v.vtype || "int")}</tspan> <tspan class="decl-n">${esc(lines[i])}</tspan></text></g>`);
        });
        sx += w + 30;
        sh = Math.max(sh, h);
      }
      for (const nt of hnotes) {
        const nl = noteLayout({ note: nt.text });
        P(`<g class="nd k-note" data-sel="${nt.id}" data-ids="${nt.id}">${shapePath("note", sx, iy, nl.w, nl.h)}` +
          nl.lines.map((l, i) => `<text x="${f1(sx + 8)}" y="${iy + 16 + i * 14}">${esc(l)}</text>`).join("") + `</g>`);
        sx += nl.w + 30;
        sh = Math.max(sh, nl.h);
      }
      for (const R of handlers) {
        R.draw(sx + R.l, iy);
        sx += R.l + R.r + 36;
        sh = Math.max(sh, R.h);
      }
      if (OPTS.edit) {
        const bx = sx;
        sx += addButton(bx, iy, "+ handler", "state-handler", st.name) + 16;
        sx = Math.max(sx, bx + addButton(bx, iy + 28, "+ variable", "state-var", st.name) + 16);
        sh = Math.max(sh, 50);
      }
      const bw = Math.max(sx - M - 36 + pad + 18, 260), bh = sh + 36 + 18;
      OUT[boxStart] = `<g class="nd-state" data-state="${esc(st.name)}"><rect class="state-box${cur ? " cur" : ""}" x="${M}" y="${top}" width="${f1(bw)}" height="${f1(bh)}" rx="12"/></g>`;
      stateBoxes[st.name] = { x: M, y: top, w: bw, h: bh };
      width = Math.max(width, M + bw);
      y += bh + 26;
    });

    const W = Math.ceil(width + M), H = Math.ceil(y + M);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">` + DEFS + OUT.join("") + `</svg>`;
    return { svg, width: W, height: H, blocks: BLOCKS, stateBoxes };
  }

  const DEFS = `<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 1 L10 5 L0 9 z" fill="#8a94a8"/></marker>` +
    `<marker id="arr2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 1 L10 5 L0 9 z" fill="#a8b2c5"/></marker></defs>`;

  // ---------------------------------------------------------------- state diagram
  function gotosIn(stmts, acc) {
    for (const s of stmts || []) {
      if (s.type === "goto") acc.push(s);
      if (s.type === "if") { s.branches.forEach((b) => gotosIn(b.body, acc)); gotosIn(s.else_body, acc); }
      if (s.type === "while") gotosIn(s.body, acc);
    }
    return acc;
  }

  function renderStates(ast, opts) {
    OPTS = opts || {};
    OUT = [];
    const states = ast.items.filter((i) => i.type === "state");
    if (!states.length) {
      return { svg: `<svg xmlns="http://www.w3.org/2000/svg" width="420" height="80"><text x="20" y="40" style="fill:#7d8696">This program has no states.</text></svg>` };
    }
    const n = states.length;
    const RAD = 58;
    const ring = n <= 1 ? 0 : Math.max(150, (n * (RAD * 2 + 70)) / (2 * Math.PI));
    const cx0 = ring + RAD + 150, cy0 = ring + RAD + 70;
    const pos = {};
    states.forEach((s, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / n + (n === 2 ? Math.PI / 2 : 0);
      pos[s.name] = { x: cx0 + ring * Math.cos(a), y: cy0 + ring * Math.sin(a) };
    });
    const edges = {};
    const addEdge = (from, to, label) => {
      const k = from + "→" + to;
      (edges[k] = edges[k] || { from, to, labels: [] }).labels.push(label);
    };
    for (const it of ast.items) {
      if (it.type === "state") for (const h of it.handlers) if (h.type === "on") gotosIn(h.body, []).forEach((g) => addEdge(it.name, g.state, h.event));
    }
    const ptOf = (name) => pos[name];
    for (const e of Object.values(edges)) {
      const a = ptOf(e.from), b = ptOf(e.to);
      if (!a || !b) continue;
      const label = esc(clip([...new Set(e.labels)].join(", "), 30));
      if (e.from === e.to) {
        P(`<path class="sd-edge" d="M${f1(a.x - 20)} ${f1(a.y - RAD + 4)} C${f1(a.x - 50)} ${f1(a.y - RAD - 70)} ${f1(a.x + 50)} ${f1(a.y - RAD - 70)} ${f1(a.x + 20)} ${f1(a.y - RAD + 4)}" marker-end="url(#arr2)"/>`);
        P(`<text class="sd-label" x="${f1(a.x)}" y="${f1(a.y - RAD - 56)}" text-anchor="middle">${label}</text>`);
        continue;
      }
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const ux = dx / d, uy = dy / d, nx = -uy, ny = ux;
      const off = 26;
      const r0 = RAD, r1 = RAD + 2;
      const sx = a.x + ux * r0 + nx * 10, sy = a.y + uy * r0 + ny * 10;
      const ex = b.x - ux * r1 + nx * 10, ey = b.y - uy * r1 + ny * 10;
      const mx = (sx + ex) / 2 + nx * off, my = (sy + ey) / 2 + ny * off;
      P(`<path class="sd-edge" d="M${f1(sx)} ${f1(sy)} Q${f1(mx)} ${f1(my)} ${f1(ex)} ${f1(ey)}" marker-end="url(#arr2)"/>`);
      P(`<text class="sd-label" x="${f1(mx)}" y="${f1(my)}" text-anchor="middle">${label}</text>`);
    }
    states.forEach((s, i) => {
      const p = pos[s.name];
      const cur = OPTS.curState === s.name;
      const evs = s.handlers.filter((h) => h.type === "on").map((h) => h.event);
      P(`<g class="nd sd-state${cur ? " cur" : ""}" data-state="${esc(s.name)}"><circle class="sh" cx="${f1(p.x)}" cy="${f1(p.y)}" r="${RAD}"/>` +
        `<text x="${f1(p.x)}" y="${f1(p.y - 4)}" text-anchor="middle" style="font-weight:bold;font-size:13px">${esc(s.name)}</text>` +
        `<text class="small" x="${f1(p.x)}" y="${f1(p.y + 13)}" text-anchor="middle">${esc(clip(evs.join(" · "), 18))}</text></g>`);
      if (s === (states.find((t) => t.initial) || states[0])) {
        P(`<circle class="initial" cx="${f1(p.x - RAD - 46)}" cy="${f1(p.y)}" r="7"/><path class="sd-edge" d="M${f1(p.x - RAD - 39)} ${f1(p.y)} H${f1(p.x - RAD - 3)}" marker-end="url(#arr2)"/>`);
      }
    });
    const W = Math.ceil(cx0 * 2 + 40), H = Math.ceil(cy0 + ring + RAD + 60);
    return { svg: `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">` + DEFS + OUT.join("") + `</svg>` + stateEventTable(ast, states) };
  }

  // state x event table: who handles each event in each state
  function stateEventTable(ast, states) {
    const events = ast.items.filter((i) => i.type === "event" || i.type === "timer").map((i) => ({ name: i.name, timer: i.type === "timer" }));
    const rows = [{ name: "boot", life: true }, { name: "enter", life: true }, { name: "exit", life: true }].concat(events);
    const head = `<tr><th>event</th>${states.map((s) => `<th>${esc(s.name)}</th>`).join("")}</tr>`;
    const body = rows.map((ev) => {
      const cells = states.map((st) => {
        const own = st.handlers.find((h) => h.type === "on" && h.event === ev.name);
        if (own) return `<td class="se-own" data-sel="${own.id}" data-goto-node="${own.id}" title="handled by state ${esc(st.name)} (line ${own.line})">● handled</td>`;
        return `<td class="se-none" title="${ev.life ? "no handler" : "event is dropped in this state"}">–</td>`;
      }).join("");
      return `<tr><td class="se-ev">${ev.life ? `<i>${ev.name}</i>` : esc(ev.name)}${ev.timer ? ' <span class="muted">timer</span>' : ""}</td>${cells}</tr>`;
    }).join("");
    return `<div class="se-wrap"><div class="se-title">Who handles each event <span class="muted">click a cell to show the handler</span></div><table class="se-table">${head}${body}</table></div>`;
  }

  return { render, renderStates, exprStr, isGate };
})();
