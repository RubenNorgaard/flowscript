/* Visual editing of the FlowScript model (AST JSON).
 *
 * The chart edits the AST in place, then sends it to the server (/api/from_model),
 * which prints canonical source, re-parses and recompiles it. Script and chart are
 * therefore always two views of the same model.
 */
"use strict";

const Edit = (function () {
  let App = null;
  let index = {};        // id -> {node, arr, idx, parent, key}
  let selId = null;
  let selBranch = 0;
  let tmpId = -1;

  const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const KEYWORDS = new Set("flow version extern event timer const var func on state initial any_state pass if elif else while goto_state raise_event start every stop return and or xor not true false".split(" "));
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // --------------------------------------------------------------- model index
  function reindex() {
    index = {};
    const ast = App.ast();
    if (!ast) return;
    (function walk(o, parent, key, arr, idx) {
      if (Array.isArray(o)) { o.forEach((v, i) => walk(v, parent, i, o, i)); return; }
      if (!o || typeof o !== "object") return;
      if (o.id !== undefined && o.type) index[o.id] = { node: o, parent, key, arr, idx };
      for (const k of Object.keys(o)) {
        const v = o[k];
        if (v && typeof v === "object") walk(v, o, k, Array.isArray(v) ? v : null, null);
      }
    })(ast, null, null, null, null);
  }

  function pathTo(target) {
    const ast = App.ast();
    let found = null;
    (function walk(o, path) {
      if (found || !o || typeof o !== "object") return;
      if (o === target) { found = path.slice(); return; }
      for (const k of Object.keys(o)) if (o[k] && typeof o[k] === "object") { path.push(k); walk(o[k], path); path.pop(); }
    })(ast, []);
    return found;
  }
  function resolve(path) {
    let o = App.ast();
    for (const k of path || []) { if (!o) return null; o = o[k]; }
    return o;
  }

  // --------------------------------------------------------------- constructors
  const nid = () => tmpId--;
  const num = (v) => ({ id: nid(), type: "num", line: 0, value: v, text: String(v) });
  const nameE = (n) => ({ id: nid(), type: "name", line: 0, name: n });
  const boolE = (v) => ({ id: nid(), type: "bool", line: 0, value: v ? 1 : 0 });
  const decls = (t) => App.ast().items.filter((i) => i.type === t);
  function uniqueName(base) {
    const used = new Set();
    for (const id in index) { const n = index[id].node; if (n.name) used.add(n.name); }
    for (let i = 1; ; i++) if (!used.has(base + i)) return base + i;
  }

  function stmtTemplate(type) {
    const ext = decls("extern"), timers = decls("timer"), events = decls("event"), states = decls("state"), vars = decls("var");
    const base = { id: nid(), line: 0, type };
    switch (type) {
      case "assign": return Object.assign(base, { target: vars.length ? vars[0].name : "x", value: num(0) });
      case "local": return Object.assign(base, { name: uniqueName("tmp"), value: num(0) });
      case "call": {
        const f = ext[0] || decls("func")[0];
        return Object.assign(base, { name: f ? f.name : "fn", args: f ? f.params.map(() => num(0)) : [] });
      }
      case "if": return Object.assign(base, { branches: [{ cond: boolE(true), body: [] }], else_body: null });
      case "while": return Object.assign(base, { cond: boolE(false), body: [] });
      case "goto": return Object.assign(base, { state: states.length ? states[0].name : "somewhere" });
      case "emit": { const e = events[0]; return Object.assign(base, { event: e ? e.name : "something", args: e ? e.params.map(() => num(0)) : [] }); }
      case "start": return Object.assign(base, { timer: timers.length ? timers[0].name : "t", every: false, ms: { id: nid(), type: "num", line: 0, value: 1000, text: "1s" } });
      case "stop": return Object.assign(base, { timer: timers.length ? timers[0].name : "t" });
      case "return": return Object.assign(base, { value: null });
      case "comment": return Object.assign(base, { text: "note" });
    }
  }

  const STMT_MENU = [
    ["assign", "Process", "x = value"], ["local", "Local variable", "var tmp = 0"], ["call", "Call (I/O / sub-process)", "f(args)"],
    ["if", "Decision", "if … else"], ["while", "Loop", "while …"], ["goto", "State transition", "goto_state S"],
    ["emit", "Raise event", "raise_event E"], ["start", "Start timer", "start T 1s"], ["stop", "Stop timer", "stop T"],
    ["return", "End / return", "return"], ["comment", "Annotation", "# note"],
  ];

  // --------------------------------------------------------------- commit
  async function commit(selectNode) {
    const path = selectNode ? pathTo(selectNode) : null;
    const res = await App.fromModel();
    if (!res) return;
    reindex();
    if (path) {
      const n = resolve(path);
      if (n && n.id !== undefined) { select(n.id, selBranch); return; }
    }
    close();
  }

  // --------------------------------------------------------------- menus
  function showMenu(x, y, entries) {
    const m = $("#menu");
    m.innerHTML = entries.map((e, i) => `<div data-i="${i}">${esc(e[0])}<span class="muted">${esc(e[1] || "")}</span></div>`).join("");
    m.classList.remove("hidden");
    const w = 220;
    m.style.left = Math.min(x, window.innerWidth - w - 10) + "px";
    m.style.top = Math.min(y, window.innerHeight - entries.length * 26 - 20) + "px";
    m.onclick = (ev) => {
      const d = ev.target.closest("[data-i]");
      if (!d) return;
      hideMenu();
      entries[+d.dataset.i][2]();
    };
    setTimeout(() => document.addEventListener("mousedown", outside, { once: true }), 0);
    function outside(ev) { if (!m.contains(ev.target)) hideMenu(); else document.addEventListener("mousedown", outside, { once: true }); }
  }
  function hideMenu() { $("#menu").classList.add("hidden"); }

  function insertMenu(blockRef, index_, x, y) {
    showMenu(x, y, STMT_MENU.map(([t, label, hint]) => [label, hint, () => {
      const arr = blockArray(blockRef, true);
      const st = stmtTemplate(t);
      arr.splice(index_, 0, st);
      commit(st);
    }]));
  }

  function blockArray(ref, create) {
    const n = ref.node;
    if (ref.key === "branch") return n.branches[ref.bi].body;
    if (ref.key === "else_body") { if (!n.else_body && create) n.else_body = []; return n.else_body; }
    return n[ref.key];
  }

  function addMenu(action, arg, x, y) {
    const ast = App.ast();
    const items = ast.items;
    const evNames = () => ["boot"].concat(decls("event").map((e) => e.name), decls("timer").map((t) => t.name));
    const lastIdx = (pred) => { let k = -1; items.forEach((it, i) => { if (pred(it)) k = i; }); return k; };
    const insertItem = (it, afterPred) => { const k = lastIdx(afterPred); items.splice(k + 1, 0, it); commit(it); };
    if (action === "any-handler") {
      let any = items.find((i) => i.type === "any_state");
      const used = new Set(any ? any.handlers.map((h) => h.event) : []);
      showMenu(x, y, evNames().filter((e) => !used.has(e)).map((e) => ["on " + e, "in every state", () => {
        const h = { id: nid(), type: "on", line: 0, event: e, params: null, body: [] };
        if (!any) {
          any = { id: nid(), type: "any_state", line: 0, handlers: [] };
          const k = items.findIndex((i) => i.type === "state" || i.type === "func");
          items.splice(k < 0 ? items.length : k, 0, any);
        }
        any.handlers.push(h);
        commit(h);
      }]));
    } else if (action === "state-handler") {
      const st = items.find((i) => i.type === "state" && i.name === arg);
      const used = new Set(st.handlers.map((h) => h.event));
      showMenu(x, y, ["enter", "exit"].concat(evNames().filter((e) => e !== "boot")).filter((e) => !used.has(e)).map((e) => ["on " + e, "in " + arg, () => {
        const h = { id: nid(), type: "on", line: 0, event: e, params: null, body: [] };
        st.handlers.push(h);
        commit(h);
      }]));
    } else if (action === "state-var") {
      const st = items.find((i) => i.type === "state" && i.name === arg);
      st.vars = st.vars || [];
      const v = { id: nid(), type: "var", line: 0, name: uniqueName("count"), value: num(0) };
      st.vars.push(v);
      commit(v);
    } else if (action === "func") {
      insertItem({ id: nid(), type: "func", line: 0, name: uniqueName("func"), params: [], body: [] }, (i) => ["extern", "event", "timer", "const", "var", "func", "any_state"].includes(i.type));
    } else if (action === "state") {
      // two or more states need a marked start state: keep the current first one as the start
      const sts = decls("state");
      if (sts.length && !sts.some((s) => s.initial)) sts[0].initial = true;
      insertItem({ id: nid(), type: "state", line: 0, name: uniqueName("state"), vars: [], handlers: [] }, () => true);
    } else if (action === "decl") {
      const DECL = ["extern", "event", "timer", "const", "var"];
      const mk = (type, extra) => () => {
        const it = Object.assign({ id: nid(), type, line: 0 }, extra);
        let k = lastIdx((i) => i.type === type);
        if (k < 0) k = lastIdx((i) => DECL.includes(i.type));
        items.splice(k + 1, 0, it);
        commit(it);
      };
      showMenu(x, y, [
        ["extern", "host function", mk("extern", { name: uniqueName("host_fn"), params: [] })],
        ["event", "host/script event", mk("event", { name: uniqueName("event"), params: [] })],
        ["timer", "VM timer", mk("timer", { name: uniqueName("timer") })],
        ["const", "constant", mk("const", { name: uniqueName("CONST"), value: num(0) })],
        ["var", "global variable", mk("var", { name: uniqueName("var"), value: num(0) })],
      ]);
    }
  }

  // --------------------------------------------------------------- inspector
  async function parseExprs(texts) {
    const r = await App.api("/api/parse_exprs", { texts });
    return r.results;
  }

  function field(label, key, value, kind, options) {
    const id = "f-" + key;
    if (kind === "select") {
      return `<label for="${id}">${esc(label)}</label><select id="${id}" data-k="${key}">` +
        options.map((o) => `<option${o === value ? " selected" : ""}>${esc(o)}</option>`).join("") + `</select>`;
    }
    if (kind === "check") return `<label><input type="checkbox" id="${id}" data-k="${key}"${value ? " checked" : ""}> ${esc(label)}</label>`;
    if (kind === "area") return `<label for="${id}">${esc(label)}</label><textarea id="${id}" data-k="${key}" rows="2">${esc(value || "")}</textarea>`;
    return `<label for="${id}">${esc(label)}</label><input type="text" id="${id}" data-k="${key}" value="${esc(value == null ? "" : value)}">`;
  }

  const S = FC.exprStr;
  const listStr = (a) => (a || []).map(S).join(", ");

  function formFor(n, branch) {
    const states = decls("state").map((s) => s.name), timers = decls("timer").map((t) => t.name);
    const events = decls("event").map((e) => e.name);
    const callables = decls("extern").map((e) => e.name).concat(decls("func").map((f) => f.name));
    const f = [];
    let title = n.type;
    switch (n.type) {
      case "assign": title = "Process (assignment)"; f.push(field("variable", "target", n.target), field("value", "value", S(n.value))); break;
      case "local": title = "Local variable"; f.push(field("name", "name", n.name), field("initial value", "value", n.value ? S(n.value) : "")); break;
      case "call": title = "Call"; f.push(field("function", "name", n.name, "select", callables.includes(n.name) ? callables : [n.name].concat(callables)), field("arguments (comma separated)", "args", listStr(n.args))); break;
      case "if":
        title = "Decision";
        n.branches.forEach((b, i) => f.push(field((i ? "elif" : "if") + " condition", "cond" + i, S(b.cond))));
        break;
      case "while": title = "Loop"; f.push(field("while condition", "cond", S(n.cond))); break;
      case "goto": title = "State transition"; f.push(field("target state", "state", n.state, "select", states.includes(n.state) ? states : [n.state].concat(states))); break;
      case "emit": title = "Raise event"; f.push(field("event", "event", n.event, "select", events.includes(n.event) ? events : [n.event].concat(events)), field("arguments", "args", listStr(n.args))); break;
      case "start": title = "Start timer"; f.push(field("timer", "timer", n.timer, "select", timers.includes(n.timer) ? timers : [n.timer].concat(timers)), field("milliseconds (e.g. 500ms, 30s, 5min)", "ms", S(n.ms)), field("periodic (every)", "every", n.every, "check")); break;
      case "stop": title = "Stop timer"; f.push(field("timer", "timer", n.timer, "select", timers.includes(n.timer) ? timers : [n.timer].concat(timers))); break;
      case "return": title = "Return"; f.push(field("value (functions only, optional)", "value", n.value ? S(n.value) : "")); break;
      case "comment": title = "Annotation"; f.push(field("text", "text", n.text, "area")); break;
      case "on": {
        title = "Event handler";
        const st = index[n.id] && index[n.id].parent && index[n.id].parent.type === "state";
        const evs = (st ? ["enter", "exit"] : ["boot"]).concat(events, timers);
        f.push(field("event", "event", n.event, "select", evs.includes(n.event) ? evs : [n.event].concat(evs)), field("parameter names (optional)", "params", (n.params || []).join(", ")));
        break;
      }
      case "func": title = "Function"; f.push(field("name", "name", n.name), field("parameters", "params", n.params.join(", "))); break;
      case "state": title = "State"; f.push(field("name", "name", n.name), field("initial state (the program starts here)", "initial", !!n.initial, "check")); break;
      case "extern": title = "Host function (extern)"; f.push(field("name", "name", n.name), field("parameters", "params", n.params.join(", "))); break;
      case "event": title = "Event"; f.push(field("name", "name", n.name), field("parameters", "params", n.params.join(", "))); break;
      case "timer": title = "Timer"; f.push(field("name", "name", n.name)); break;
      case "const": title = "Constant"; f.push(field("name", "name", n.name), field("value", "value", S(n.value))); break;
      case "var": title = index[n.id] && index[n.id].key === "vars" ? "State variable (reset on entry)" : "Global variable"; f.push(field("name", "name", n.name), field("initial value", "value", n.value ? S(n.value) : "")); break;
      default:
        if (n.type === "binary" || n.type === "unary" || n.type === "num" || n.type === "name" || n.type === "callx" || n.type === "bool" || n.type === "str") {
          title = FC.isGate(n) ? (n.type === "unary" ? "NOT gate" : n.op.toUpperCase() + " gate") : "Condition input";
          f.push(field("expression", "expr", S(n)));
          f.push(field("operand for AND / OR / XOR", "operand", "true"));
        }
    }
    if (["assign", "local", "call", "if", "while", "goto", "emit", "start", "stop", "return", "on", "func", "state", "extern", "event", "timer", "const", "var"].includes(n.type)) {
      f.push(field("annotation (note)", "note", n.note || "", "area"));
    }
    return { title, html: f.join("") };
  }

  function isExpr(n) { return ["binary", "unary", "num", "name", "callx", "bool", "str"].includes(n.type); }
  function isStmt(n) { return ["assign", "local", "call", "if", "while", "goto", "emit", "start", "stop", "return", "comment"].includes(n.type); }

  function select(id, branch) {
    reindex();
    const ent = index[id];
    if (!ent) { close(); return; }
    selId = id;
    selBranch = branch || 0;
    const n = ent.node;
    const { title, html } = formFor(n, selBranch);
    const btn = (act, label, cls) => `<button data-act="${act}"${cls ? ` class="${cls}"` : ""}>${label}</button>`;
    let actions = [btn("apply", "Apply", "primary")];
    if (isStmt(n)) actions.push(btn("up", "↑"), btn("down", "↓"), btn("before", "+ before"), btn("after", "+ after"), btn("dup", "Duplicate"));
    if (n.type === "if") actions.push(btn("elif", "+ elif"), n.else_body ? btn("noelse", "− else") : btn("else", "+ else"), n.branches.length > 1 ? btn("rmbranch", "− last elif") : "");
    if (isExpr(n)) {
      actions.push(btn("not", n.type === "unary" && n.op === "not" ? "Remove NOT" : "Wrap in NOT"), btn("and", "AND …"), btn("or", "OR …"), btn("xor", "XOR …"));
      const p = ent.parent;
      if (p && p.type === "binary" && ["and", "or", "xor"].includes(p.op)) actions.push(btn("rminput", "Remove input"));
      if (n.type === "binary" && ["and", "or", "xor"].includes(n.op)) actions.push(btn("op-and", "→AND"), btn("op-or", "→OR"), btn("op-xor", "→XOR"));
    }
    if (!isExpr(n)) actions.push(btn("delete", "Delete", "danger"));
    const box = $("#inspector");
    box.innerHTML = `<h4>${esc(title)} <button data-act="close" class="small">✕</button></h4>${html}<div class="row">${actions.join("")}</div><div class="err" id="insp-err"></div>` +
      `<div class="hint">Changes are applied to the model; the script is regenerated from it.</div>`;
    box.classList.remove("hidden");
    box.onclick = (ev) => { const b = ev.target.closest("[data-act]"); if (b) act(b.dataset.act, ev); };
    box.onkeydown = (ev) => { if (ev.key === "Enter" && ev.target.tagName === "INPUT") { ev.preventDefault(); act("apply", ev); } };
    App.markSelected(id);
  }

  function close() {
    selId = null;
    $("#inspector").classList.add("hidden");
    App.markSelected(null);
  }

  function val(k) { const el = document.querySelector(`#inspector [data-k="${k}"]`); return el ? (el.type === "checkbox" ? el.checked : el.value) : undefined; }
  function err(msg) { $("#insp-err").textContent = msg || ""; }
  function checkName(n, what) {
    if (!NAME_RE.test(n)) throw new Error(`'${n}' is not a valid ${what || "name"}`);
    if (KEYWORDS.has(n)) throw new Error(`'${n}' is a keyword`);
    return n;
  }
  function params(text) {
    const t = (text || "").trim();
    if (!t) return [];
    return t.split(",").map((s) => checkName(s.trim(), "parameter name"));
  }
  async function exprs(map) {
    // map: key -> text ; returns key -> expr (throws on error)
    const keys = Object.keys(map);
    const res = await parseExprs(keys.map((k) => map[k]));
    const out = {};
    keys.forEach((k, i) => { if (!res[i].ok) throw new Error(`${k}: ${res[i].error}`); out[k] = res[i].expr; });
    return out;
  }
  async function exprList(text) {
    const t = (text || "").trim();
    if (!t) return [];
    // split on top-level commas
    const parts = [];
    let depth = 0, cur = "", inStr = false;
    for (const ch of t) {
      if (ch === '"') inStr = !inStr;
      if (!inStr && ch === "(") depth++;
      if (!inStr && ch === ")") depth--;
      if (!inStr && ch === "," && depth === 0) { parts.push(cur); cur = ""; } else cur += ch;
    }
    parts.push(cur);
    const res = await parseExprs(parts.map((p) => p.trim()));
    return res.map((r, i) => { if (!r.ok) throw new Error(`argument ${i + 1}: ${r.error}`); return r.expr; });
  }

  async function apply(n) {
    const note = val("note");
    if (note !== undefined) { if (note.trim()) n.note = note.replace(/\s+$/, ""); else delete n.note; }
    switch (n.type) {
      case "assign": n.target = checkName(val("target").trim(), "variable"); n.value = (await exprs({ value: val("value") })).value; break;
      case "local": {
        n.name = checkName(val("name").trim());
        const v = val("value").trim();
        n.value = v ? (await exprs({ value: v })).value : null;
        break;
      }
      case "call": n.name = val("name"); n.args = await exprList(val("args")); break;
      case "if": {
        const m = {};
        n.branches.forEach((b, i) => (m["cond" + i] = val("cond" + i)));
        const r = await exprs(m);
        n.branches.forEach((b, i) => (b.cond = r["cond" + i]));
        break;
      }
      case "while": n.cond = (await exprs({ cond: val("cond") })).cond; break;
      case "goto": n.state = val("state"); break;
      case "emit": n.event = val("event"); n.args = await exprList(val("args")); break;
      case "start": n.timer = val("timer"); n.every = !!val("every"); n.ms = (await exprs({ ms: val("ms") })).ms; break;
      case "stop": n.timer = val("timer"); break;
      case "return": { const v = val("value").trim(); n.value = v ? (await exprs({ value: v })).value : null; break; }
      case "comment": n.text = val("text"); break;
      case "on": { n.event = val("event"); const p = val("params").trim(); n.params = p ? params(p) : null; break; }
      case "func": case "extern": n.name = checkName(val("name").trim()); n.params = params(val("params")); break;
      case "event": n.name = checkName(val("name").trim()); n.params = params(val("params")); break;
      case "state": {
        const old = n.name;
        n.name = checkName(val("name").trim());
        if (old !== n.name) renameGotos(old, n.name);
        if (val("initial")) { decls("state").forEach((s) => { delete s.initial; }); n.initial = true; }
        else if (n.initial) delete n.initial;
        break;
      }
      case "timer": n.name = checkName(val("name").trim()); break;
      case "const": n.name = checkName(val("name").trim()); n.value = (await exprs({ value: val("value") })).value; break;
      case "var": { n.name = checkName(val("name").trim()); const v = val("value").trim(); n.value = v ? (await exprs({ value: v })).value : null; break; }
      default:
        if (isExpr(n)) { const e = (await exprs({ expr: val("expr") })).expr; replaceExpr(n, e); return e; }
    }
    return n;
  }

  function renameGotos(from, to) {
    (function walk(o) {
      if (Array.isArray(o)) return o.forEach(walk);
      if (!o || typeof o !== "object") return;
      if (o.type === "goto" && o.state === from) o.state = to;
      for (const k of Object.keys(o)) if (o[k] && typeof o[k] === "object") walk(o[k]);
    })(App.ast());
  }

  function replaceExpr(oldE, newE) {
    const ent = index[oldE.id];
    if (ent.arr && ent.idx !== null && ent.idx !== undefined) ent.arr[ent.idx] = newE;
    else ent.parent[ent.key] = newE;
  }

  async function act(a, ev) {
    const ent = index[selId];
    if (a === "close") return close();
    if (!ent) return;
    const n = ent.node;
    try {
      err("");
      if (a === "apply") { const r = await apply(n); return commit(r); }
      if (a === "delete") {
        if (ent.arr) ent.arr.splice(ent.idx, 1);
        return commit(null);
      }
      if (a === "up" || a === "down") {
        const j = ent.idx + (a === "up" ? -1 : 1);
        if (j < 0 || j >= ent.arr.length) return;
        [ent.arr[ent.idx], ent.arr[j]] = [ent.arr[j], ent.arr[ent.idx]];
        return commit(n);
      }
      if (a === "before" || a === "after") {
        const r = ev.target.getBoundingClientRect();
        return showMenu(r.left, r.bottom + 4, STMT_MENU.map(([t, label, hint]) => [label, hint, () => {
          const st = stmtTemplate(t);
          ent.arr.splice(ent.idx + (a === "after" ? 1 : 0), 0, st);
          commit(st);
        }]));
      }
      if (a === "dup") { const c = JSON.parse(JSON.stringify(n)); ent.arr.splice(ent.idx + 1, 0, c); return commit(c); }
      if (a === "elif") { n.branches.push({ cond: boolE(true), body: [] }); return commit(n); }
      if (a === "rmbranch") { n.branches.pop(); return commit(n); }
      if (a === "else") { n.else_body = []; return commit(n); }
      if (a === "noelse") { n.else_body = null; return commit(n); }
      if (a === "not") {
        const e = n.type === "unary" && n.op === "not" ? n.operand : { id: nid(), type: "unary", line: 0, op: "not", operand: n };
        replaceExpr(n, e);
        return commit(e);
      }
      if (a === "and" || a === "or" || a === "xor") {
        const operand = (await exprs({ operand: val("operand") || "true" })).operand;
        const e = { id: nid(), type: "binary", line: 0, op: a, left: n, right: operand };
        replaceExpr(n, e);
        return commit(e);
      }
      if (a.startsWith("op-")) { n.op = a.slice(3); return commit(n); }
      if (a === "rminput") {
        const p = ent.parent;
        const sibling = p.left === n ? p.right : p.left;
        replaceExpr(p, sibling);
        return commit(sibling);
      }
    } catch (e) {
      err(e.message || String(e));
    }
  }

  // --------------------------------------------------------------- wiring
  function init(app) {
    App = app;
    const chart = $("#chart");
    chart.addEventListener("click", (ev) => {
      if (!App.editMode()) return;
      const plusEl = ev.target.closest(".plus");
      if (plusEl) {
        const ref = App.blocks()[+plusEl.dataset.block];
        insertMenu(ref, +plusEl.dataset.index, ev.clientX, ev.clientY);
        ev.stopPropagation();
        return;
      }
      const add = ev.target.closest(".addbtn");
      if (add) { addMenu(add.dataset.add, add.dataset.arg, ev.clientX, ev.clientY); ev.stopPropagation(); return; }
      const st = ev.target.closest("[data-state]");
      const nd = ev.target.closest("[data-sel]");
      if (nd) { select(+nd.dataset.sel, +(nd.dataset.branch || 0)); ev.stopPropagation(); return; }
      if (st) {
        const s = decls("state").find((x) => x.name === st.dataset.state);
        if (s) { select(s.id); ev.stopPropagation(); }
      }
    });
  }

  return { init, select, close, reindex, get selected() { return selId; } };
})();
