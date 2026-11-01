/* FlowScript debugger front end: wires the three views to the server. */
"use strict";

(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  let data = null;          // last compile result
  let snap = null;          // last VM snapshot
  let prevGlobals = {};
  let breakLines = new Set();
  let zoom = 1;
  let chartMode = "flow";
  let blocks = [];
  let dirty = false;
  let selectedNode = null;
  let busy = false;

  // ------------------------------------------------------------------ server
  async function api(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json();
    if (j.error) setStatus(j.error, "bad");
    return j;
  }

  function setStatus(text, cls) {
    const s = $("#status");
    s.textContent = text;
    s.className = "status " + (cls || "");
  }

  // ------------------------------------------------------------------ source editor
  const KW = new Set("flow version extern event timer const int float str time func on state initial any_state pass if elif else while goto_state raise_event start every stop return and or xor not true false".split(" "));
  function highlightLine(line) {
    let out = "";
    const re = /(#.*$)|("(?:[^"\\]|\\.)*"?)|(\b\d[\w]*(?:\.\d[\d_]*)?(?:[eE][-+]?\d+)?\b)|([A-Za-z_]\w*)(\s*\()?|(->|==|!=|<=|>=|<<|>>|[-+*/%&|^~<>=(),:])|(\s+)|(.)/g;
    let m;
    while ((m = re.exec(line))) {
      if (m[1]) out += `<span class="tk-cmt">${esc(m[1])}</span>`;
      else if (m[2]) out += `<span class="tk-str">${esc(m[2])}</span>`;
      else if (m[3]) out += `<span class="tk-num">${esc(m[3])}</span>`;
      else if (m[4]) {
        if (KW.has(m[4])) out += `<span class="tk-kw">${m[4]}</span>`;
        else if (m[5]) out += `<span class="tk-fn">${m[4]}</span>`;
        else out += esc(m[4]);
        if (m[5]) out += esc(m[5]);
      } else if (m[6]) out += `<span class="tk-op">${esc(m[6])}</span>`;
      else out += esc(m[0]);
      if (m[0] === "") re.lastIndex++;
    }
    return out;
  }

  function renderSource() {
    const text = $("#code").value;
    const lines = text.split("\n");
    const errLines = new Set((data && data.errors || []).map((e) => e.line));
    const cur = snap && snap.active ? snap.line : null;
    $("#code-hl").innerHTML = lines.map((l, i) => {
      const n = i + 1;
      const cls = "ln" + (n === cur ? " cur" : "") + (errLines.has(n) ? " err" : "");
      return `<span class="${cls}" data-line="${n}">${highlightLine(l) || " "}</span>`;
    }).join("");
    $("#gutter").innerHTML = lines.map((_, i) => {
      const n = i + 1;
      const cls = (breakLines.has(n) ? "bp " : "") + (n === cur ? "cur " : "") + (errLines.has(n) ? "err" : "");
      return `<div class="${cls}" data-line="${n}">${n}</div>`;
    }).join("");
  }

  function scrollToLine(line) {
    if (!line) return;
    const ed = $("#editor");
    const y = 6 + (line - 1) * 19;
    if (y < ed.scrollTop + 20 || y > ed.scrollTop + ed.clientHeight - 40) ed.scrollTop = Math.max(0, y - ed.clientHeight / 3);
  }

  // ------------------------------------------------------------------ bytecode view
  function renderBytecode() {
    const el = $("#bytecode");
    if (!data || !data.disasm) {
      el.innerHTML = `<div class="header">no bytecode (fix the errors first)</div>`;
      $("#bc-info").textContent = "";
      return;
    }
    $("#bc-info").textContent = `${data.code_size} B code · ${data.size} B image`;
    const rows = [];
    for (const ins of data.disasm) {
      if (ins.label) rows.push(`<div class="lbl">${esc(ins.label)}:</div>`);
      const arg = ins.arg === null || ins.arg === undefined ? "" : ins.arg;
      rows.push(`<div class="ins" data-pc="${ins.pc}" data-node="${ins.node}" data-stmt="${ins.stmt}" data-line="${ins.line}">` +
        `<span class="mk"></span><span class="pcx">${ins.pc.toString(16).padStart(4, "0")}</span><span class="hex">${ins.hex}</span>` +
        `<span class="op">${ins.op}</span><span class="arg">${esc(arg)}${ins.comment ? ` <i>; ${esc(ins.comment)}</i>` : ""}</span></div>`);
    }
    el.innerHTML = rows.join("");
  }

  // ------------------------------------------------------------------ size view
  let bcMode = "code";
  function setBcMode(mode) {
    bcMode = mode;
    $$("#bc-mode button").forEach((x) => x.classList.toggle("on", x.dataset.mode === mode));
    $("#bytecode").classList.toggle("hidden", mode !== "code");
    $("#sizeview").classList.toggle("hidden", mode !== "size");
    if (mode === "size") renderSize();
  }

  function renderSize() {
    const el = $("#sizeview");
    const r = data && data.size_report;
    if (!r) { el.innerHTML = `<div class="header">no bytecode (fix the errors first)</div>`; return; }
    const bar = (n, total) => `<span class="sz-bar"><span style="width:${total ? (100 * n / total).toFixed(1) : 0}%"></span></span>`;
    const pct = (n, total) => total ? Math.round(100 * n / total) + "%" : "";
    const h = [];
    h.push(`<h4>Image <b>${r.image_bytes} B</b> <span class="muted">stored as the .fsb (e.g. in flash); the C VM reads it in place${r.debug_bytes ? ` · debug info would add ${r.debug_bytes} B` : ""}</span></h4>`);
    h.push(`<table class="sz">`);
    for (const s of r.sections) {
      if (!s.bytes) continue;
      h.push(`<tr><td>${esc(s.name)}</td><td class="n">${s.bytes}</td><td class="n muted">${pct(s.bytes, r.image_bytes)}</td><td>${bar(s.bytes, r.image_bytes)}</td><td class="muted">${esc(s.detail)}</td></tr>`);
    }
    h.push(`</table>`);
    const code = r.routines.reduce((a, x) => a + x.bytes, 0);
    h.push(`<h4>Code by routine <b>${code} B</b> <span class="muted">click to show the code · stack = worst-case value slots · frames = call depth</span></h4>`);
    h.push(`<table class="sz"><tr class="muted"><td></td><td class="n">bytes</td><td class="n">stack</td><td class="n">frames</td><td></td></tr>`);
    for (const x of [...r.routines].sort((a, b) => b.bytes - a.bytes)) {
      const dash = (v) => v === null || v === undefined ? "∞" : v;
      h.push(`<tr class="sz-rt" data-pc="${x.start}"><td>${x.kind === "func" ? "func " : ""}${esc(x.name)}</td><td class="n">${x.bytes}</td><td class="n">${dash(x.stack)}</td><td class="n">${dash(x.frames)}</td><td>${bar(x.bytes, code)}</td></tr>`);
    }
    h.push(`</table>`);
    const sb = r.strings.reduce((a, x) => a + x.bytes, 0);
    const roles = Object.entries(r.strings_by_role).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
    h.push(`<h4>Strings <b>${sb} B</b> <span class="muted">${roles}</span></h4><table class="sz">`);
    for (const s of r.strings) h.push(`<tr><td class="mono">"${esc(s.text)}"</td><td class="n">${s.bytes}</td><td class="muted" colspan="3">${esc(s.roles.join(", "))}</td></tr>`);
    h.push(`</table>`);
    const ram = r.ram;
    h.push(`<h4>RAM <b>${ram.minimum_bytes} B</b> <span class="muted">sizeof(fs_vm) on a 32-bit target with the minimum settings (defaults: ${ram.default_bytes} B)</span></h4>`);
    h.push(`<table class="sz"><tr class="muted"><td></td><td class="n">default</td><td class="n">minimum</td><td colspan="2"></td></tr>`);
    for (const x of ram.rows) h.push(`<tr><td class="mono">${x.macro}</td><td class="n">${x.default}</td><td class="n"><b>${x.minimum === null ? "–" : x.minimum}</b></td><td colspan="2" class="muted">${esc(x.why)}</td></tr>`);
    h.push(`</table>`);
    if (ram.cflags) h.push(`<div class="sz-flags"><span class="muted">build flags</span> <code>${esc(ram.cflags)}</code></div>`);
    h.push(`<h4>C VM code <b>~${Math.round(r.vm_code_bytes / 1024)} KB</b> <span class="muted">flowscript.c, fixed, independent of the program</span></h4>`);
    if (r.hints.length) h.push(`<h4>Hints</h4><ul class="sz-hints">${r.hints.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`);
    el.innerHTML = h.join("");
  }

  function highlightBytecode() {
    $$("#bytecode .ins.cur, #bytecode .ins.stmt").forEach((e) => e.classList.remove("cur", "stmt"));
    if (!snap || !snap.active) return;
    $$(`#bytecode .ins[data-stmt="${snap.stmt}"]`).forEach((e) => e.classList.add("stmt"));
    const row = $(`#bytecode .ins[data-pc="${snap.pc}"]`);
    if (row) {
      row.classList.add("cur");
      const box = $("#bytecode");
      const top = row.offsetTop - box.offsetTop;
      if (top < box.scrollTop + 10 || top > box.scrollTop + box.clientHeight - 30) box.scrollTop = top - box.clientHeight / 3;
    }
  }

  // ------------------------------------------------------------------ chart
  function renderChart() {
    const host = $("#chart");
    if (!data || !data.ast) { host.innerHTML = ""; return; }
    const opts = {
      edit: $("#edit-mode").checked,
      values: snap && snap.values ? snap.values : {},
      breakLines,
      curState: snap ? snap.state : null,
    };
    const res = chartMode === "flow" ? FC.render(data.ast, opts) : FC.renderStates(data.ast, opts);
    blocks = res.blocks || [];
    host.innerHTML = res.svg;
    const svg = host.querySelector("svg");
    if (svg) {
      svg.setAttribute("width", Math.round(svg.viewBox.baseVal.width * zoom));
      svg.setAttribute("height", Math.round(svg.viewBox.baseVal.height * zoom));
    }
    $("#zoom-fit").textContent = Math.round(zoom * 100) + "%";
    highlightChart();
    markSelected(selectedNode);
  }

  function routineAt(pc) {
    return (data.routines || []).find((r) => r.start <= pc && pc < r.end);
  }

  function chartTargets() {
    if (!snap || !snap.active || chartMode !== "flow") return [];
    if (snap.steps === 0) {
      const r = routineAt(snap.pc);
      return r ? $$(`#chart [data-head="${r.node}"]`) : [];
    }
    let els = $$(`#chart [data-ids~="${snap.node}"]`);
    if (!els.length && data.stmt_of) els = $$(`#chart [data-ids~="${data.stmt_of[snap.node]}"]`);
    const extra = [];
    for (const e of els) if (e.dataset.dec) extra.push(...$$(`#chart .k-dec[data-ids~="${e.dataset.dec}"]`));
    return els.concat(extra);
  }

  function highlightChart() {
    $$("#chart .cur").forEach((e) => e.classList.remove("cur"));
    const els = chartTargets();
    els.forEach((e) => e.classList.add("cur"));
    if (els.length && $("#follow").checked) scrollChartTo(els[0]);
  }

  function scrollChartTo(el) {
    const wrap = $("#chart-wrap");
    const r = el.getBoundingClientRect(), w = wrap.getBoundingClientRect();
    const margin = 60;
    if (r.left < w.left + margin || r.right > w.right - margin || r.top < w.top + margin || r.bottom > w.bottom - margin) {
      wrap.scrollLeft += r.left + r.width / 2 - (w.left + w.width / 2);
      wrap.scrollTop += r.top + r.height / 2 - (w.top + w.height / 2);
    }
  }

  function markSelected(id) {
    selectedNode = id;
    $$("#chart .sel").forEach((e) => e.classList.remove("sel"));
    if (id !== null && id !== undefined) $$(`#chart [data-sel="${id}"]`).forEach((e) => e.classList.add("sel"));
  }

  // ------------------------------------------------------------------ panels
  function renderPanels() {
    const s = snap || {};
    const kv = (rows) => `<div class="kv">${rows.map(([k, v, c]) => `<span class="k">${esc(k)}</span><span class="v ${c || ""}">${v}</span>`).join("")}</div>`;
    const m = [];
    m.push(["status", esc(s.status || "-")]);
    m.push(["state", esc(s.state || "—") + (s.pending ? ` <span class="muted">→ ${esc(s.pending)}</span>` : "")]);
    if (s.active) {
      m.push(["pc", "0x" + s.pc.toString(16).padStart(4, "0")]);
      m.push(["budget", s.steps + " / 10000"]);
    } else m.push(["handler", "<span class='muted'>idle</span>"]);
    m.push(["steps", s.total_steps || 0]);
    m.push(["queue", (s.queue || []).length ? s.queue.map((q) => esc(q.event + (q.args.length ? "(" + q.args.join(",") + ")" : ""))).join(", ") : "<span class='muted'>empty</span>"]);
    for (const t of s.timers || []) {
      const own = t.active ? (t.owner ? ` <span class='muted' title='stops when leaving state ${esc(t.owner)}'>· ${esc(t.owner)}</span>` : ` <span class='muted' title='started by a global handler: keeps running across states'>· global</span>`) : "";
      m.push(["⧗ " + t.name, t.active ? `${t.remaining} ms${t.periodic ? " ⟳ " + t.period : ""}${own}` : "<span class='muted'>stopped</span>"]);
    }
    $("#machine").innerHTML = kv(m);

    $("#globals").innerHTML = `<div class="kv">` + (s.globals || []).map((g) =>
      `<span class="k" title="${esc(g.type || "")}">${esc(g.name)}</span><span class="v"><input data-g="${esc(g.name)}" value="${esc(String(g.value))}" title="${esc(g.type || "")}" class="${prevGlobals[g.name] !== undefined && prevGlobals[g.name] !== g.value ? "chg" : ""}" style="${prevGlobals[g.name] !== undefined && prevGlobals[g.name] !== g.value ? "color:var(--cur)" : ""}"></span>`).join("") + `</div>` ||
      "<span class='muted'>none</span>";

    let fr = "";
    for (const f of (s.frames || []).slice().reverse()) {
      fr += `<div class="frame"><b>${esc(f.routine)}</b> <span class="muted">@${f.pc.toString(16).padStart(4, "0")}</span>` +
        (f.locals.length ? kv(f.locals.filter((l) => !l.name.startsWith("$")).map((l) => [l.name, l.value])) : "") + `</div>`;
    }
    fr += `<div class="stackv"><span class="muted">stack:</span> [${(s.stack || []).join(", ")}]</div>`;
    $("#frames").innerHTML = s.active ? fr : "<span class='muted'>no active handler</span>";

    const log = $("#log");
    log.innerHTML = (s.log || []).map((l) => `<div class="${l.kind}">${esc(l.text)}</div>`).join("");
    log.parentElement.scrollTop = log.parentElement.scrollHeight;
  }

  function renderExterns() {
    const ev = data && data.externs ? data.externs : [];
    $("#externs").innerHTML = ev.length ? `<div class="kv">` + ev.map((e) =>
      `<span class="k">${esc(e.name)}(${esc((e.types || []).join(", "))})</span><span class="v">` +
      (e.returns ? `<input data-x="${esc(e.name)}" value="${esc(String((data.extern_values || {})[e.name] ?? ""))}" placeholder="${esc(e.returns)}" title="${esc(e.returns)} returned by ${esc(e.name)} (empty: the zero value)">`
                 : `<span class='muted'>returns nothing</span>`) + `</span>`).join("") + `</div>`
      : "<span class='muted'>no externs</span>";
    const sel = $("#ev-select");
    const cur = sel.value;
    const evs = data && data.events ? data.events : [];
    sel.innerHTML = evs.map((e) => `<option value="${esc(e.name)}" data-argc="${e.argc}" data-types="${esc((e.types || []).join(", "))}">${esc(e.name)}${e.argc ? "(" + esc((e.types || []).join(", ")) + ")" : ""}</option>`).join("");
    if (evs.some((e) => e.name === cur)) sel.value = cur;
    argHint();
  }

  // The selected event's parameter types as the placeholder of the arguments box.
  function argHint() {
    const o = $("#ev-select").selectedOptions[0];
    const types = o ? o.dataset.types : "";
    $("#ev-args").placeholder = types ? types + '  e.g. 1, 2.5, "text", 2026-11-01T12:00:00Z' : "no arguments";
  }

  // Comma separated arguments; a "quoted string" may contain commas (\" escapes a quote).
  function splitArgs(text) {
    const out = [];
    let cur = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q && c === "\\" && i + 1 < text.length) { cur += c + text[++i]; continue; }
      if (c === '"') q = !q;
      if (c === "," && !q) { out.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    if (cur.trim() || out.length) out.push(cur.trim());
    return out;
  }

  // ------------------------------------------------------------------ apply results
  function applyCompile(res, keepEditor) {
    data = res;
    breakLines = new Set(res.breakpoints || [...breakLines]);
    if (!keepEditor) $("#code").value = res.source;
    dirty = false;
    $("#filename").textContent = res.path ? "— " + res.path : "— (unsaved)";
    $("#errors").innerHTML = (res.errors || []).map((e) => `<div data-line="${e.line}">⚠ line ${e.line}: ${esc(e.msg)}</div>`).join("") +
      (res.warnings || []).map((w) => `<div class="warn" data-line="${w.line}">△ line ${w.line}: ${esc(w.msg)}</div>`).join("");
    $("#src-info").textContent = res.errors && res.errors.length ? "" : `${res.source.split("\n").length} lines`;
    renderBytecode();
    if (bcMode === "size") renderSize();
    renderExterns();
    Edit.reindex();
    applySnap(res.snapshot, true);
    if (res.errors && res.errors.length) setStatus("compile error", "bad");
    else setStatus("compiled ✓ " + (res.snapshot && res.snapshot.state ? "state " + res.snapshot.state : "") +
      (res.warnings && res.warnings.length ? ` · ${res.warnings.length} warning${res.warnings.length > 1 ? "s" : ""}` : ""), "ok");
  }

  function applySnap(s, fresh) {
    if (!s) return;
    if (snap && snap.globals && !fresh) prevGlobals = Object.fromEntries(snap.globals.map((g) => [g.name, g.value]));
    else prevGlobals = {};
    snap = s;
    renderSource();
    if (s.active && $("#follow").checked) scrollToLine(s.line);
    highlightBytecode();
    renderChart();
    renderPanels();
    if (!fresh) {
      const st = s.status;
      const label = { idle: "idle", dispatch: "handler dispatched", running: "paused", done: "handler finished", fault: "FAULT", breakpoint: "breakpoint hit", limit: "step limit" }[st] || st;
      setStatus(label + (s.state ? " · state " + s.state : ""), st === "fault" ? "bad" : st === "breakpoint" || s.active ? "run" : "ok");
    }
  }

  async function compile() {
    const res = await api("/api/compile", { source: $("#code").value });
    if (res.source !== undefined) applyCompile(res, true);
  }

  async function fromModel() {
    const res = await api("/api/from_model", { ast: data.ast });
    if (res.source === undefined) return null;
    applyCompile(res, false);
    return res;
  }

  async function step(mode) {
    if (busy) return;
    if (dirty) await compile();
    busy = true;
    try {
      const r = await api("/api/vm/step", { mode });
      if (r.snapshot) applySnap(r.snapshot);
    } finally { busy = false; }
  }

  function parseMs(t) {
    const m = String(t).trim().match(/^(\d+)\s*(ms|s|min|h)?$/);
    if (!m) return null;
    return +m[1] * ({ ms: 1, s: 1000, min: 60000, h: 3600000 }[m[2] || "ms"]);
  }

  async function post() {
    const ev = $("#ev-select").value;
    if (!ev) return;
    const args = splitArgs($("#ev-args").value);
    if (args.some(isNaN)) return setStatus("bad event arguments", "bad");
    const r = await api("/api/vm/post", { event: ev, args });
    if (r.snapshot) applySnap(r.snapshot);
    if ($("#auto-run").checked) await step("run");
  }

  async function tick(ms) {
    if (ms === null) return setStatus("bad tick value (use e.g. 500, 30s, 5min)", "bad");
    const r = await api("/api/vm/tick", { ms });
    if (r.snapshot) applySnap(r.snapshot);
    if ($("#auto-run").checked) await step("run");
  }

  async function sendBreakpoints() {
    await api("/api/vm/breakpoints", { lines: [...breakLines] });
    renderSource();
    renderChart();
  }

  function toggleBreak(line) {
    if (!line) return;
    if (breakLines.has(line)) breakLines.delete(line); else breakLines.add(line);
    sendBreakpoints();
  }

  // ------------------------------------------------------------------ cross-view hover
  function hoverLine(line, on) {
    const el = $(`#code-hl .ln[data-line="${line}"]`);
    if (el) el.classList.toggle("hov", on);
  }
  function hoverNode(ids, on) {
    for (const id of ids) $$(`#chart [data-ids~="${id}"]`).forEach((e) => e.classList.toggle("hov", on));
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    const code = $("#code");
    let hlTimer = null;
    code.addEventListener("input", () => {
      dirty = true;
      setStatus("modified — Ctrl+Enter to compile", "run");
      clearTimeout(hlTimer);
      hlTimer = setTimeout(renderSource, 30);
    });
    code.addEventListener("keydown", (ev) => {
      const s = code.selectionStart, e = code.selectionEnd;
      const lineStart = code.value.lastIndexOf("\n", s - 1) + 1;
      const before = code.value.slice(lineStart, s);
      if (ev.key === "Tab" && ev.shiftKey) {
        // dedent the current line by one level
        ev.preventDefault();
        const n = (code.value.slice(lineStart).match(/^ {1,2}/) || [""])[0].length;
        if (n) {
          code.setRangeText("", lineStart, lineStart + n, "preserve");
          code.setSelectionRange(Math.max(lineStart, s - n), Math.max(lineStart, e - n));
          code.dispatchEvent(new Event("input"));
        }
      } else if (ev.key === "Tab") {
        ev.preventDefault();
        code.setRangeText("  ", s, e, "end");
        code.dispatchEvent(new Event("input"));
      } else if (ev.key === "Backspace" && s === e && s > lineStart && /^ +$/.test(before)) {
        // in the indentation: remove one level (2 spaces)
        ev.preventDefault();
        const n = before.length % 2 === 0 ? 2 : 1;
        code.setRangeText("", s - n, s, "end");
        code.dispatchEvent(new Event("input"));
      } else if (ev.key === "Enter" && !ev.ctrlKey && !ev.metaKey) {
        // keep indentation; one level deeper after a line ending in ':'
        let indent = before.match(/^ */)[0];
        if (/:\s*(#.*)?$/.test(before)) indent += "  ";
        ev.preventDefault();
        code.setRangeText("\n" + indent, s, e, "end");
        code.dispatchEvent(new Event("input"));
      }
    });
    $("#gutter").addEventListener("click", (ev) => { const d = ev.target.closest("[data-line]"); if (d) toggleBreak(+d.dataset.line); });
    $("#errors").addEventListener("click", (ev) => { const d = ev.target.closest("[data-line]"); if (d) scrollToLine(+d.dataset.line); });

    $("#btn-compile").onclick = compile;
    $("#btn-format").onclick = async () => {
      const r = await api("/api/format", { source: $("#code").value });
      if (r.source !== undefined) { $("#code").value = r.source; await compile(); }
    };
    $("#btn-save").onclick = async () => {
      let path = data && data.path;
      if (!path) { path = window.prompt("Save as (path on the server machine):", "program.fs"); if (!path) return; }
      const r = await api("/api/save", { source: $("#code").value, path });
      if (r.ok) { setStatus("saved " + r.path, "ok"); $("#filename").textContent = "— " + r.path; if (data) data.path = r.path; }
    };
    $("#btn-download").onclick = async () => {
      const r = await api("/api/bytecode", { debug: false });
      if (!r.base64) return;
      const bin = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([bin], { type: "application/octet-stream" }));
      a.download = ((data.ast && data.ast.name) || "program") + ".fsb";
      a.click();
    };
    $("#btn-reset").onclick = async () => { const r = await api("/api/vm/reset"); if (r.snapshot) applySnap(r.snapshot, true); setStatus("reset", "ok"); };
    $("#btn-instr").onclick = () => step("instr");
    $("#btn-stmt").onclick = () => step("stmt");
    $("#btn-over").onclick = () => step("over");
    $("#btn-out").onclick = () => step("out");
    $("#btn-handler").onclick = () => step("handler");
    $("#btn-run").onclick = () => step("run");
    $("#btn-post").onclick = post;
    $("#ev-args").addEventListener("keydown", (ev) => { if (ev.key === "Enter") post(); });
    $("#ev-select").addEventListener("change", argHint);
    $("#btn-tick").onclick = () => tick(parseMs($("#tick-ms").value));
    $("#tick-ms").addEventListener("keydown", (ev) => { if (ev.key === "Enter") tick(parseMs($("#tick-ms").value)); });
    $$("[data-tick]").forEach((b) => (b.onclick = () => tick(+b.dataset.tick)));
    $("#btn-clear-log").onclick = () => { if (snap) { snap.log = []; renderPanels(); } };

    $("#externs").addEventListener("change", async (ev) => {
      const i = ev.target.closest("[data-x]");
      if (!i) return;
      const v = i.value.trim();
      await api("/api/vm/extern", { name: i.dataset.x, value: v });
      data.extern_values = Object.assign(data.extern_values || {}, { [i.dataset.x]: v });
    });
    $("#globals").addEventListener("change", async (ev) => {
      const i = ev.target.closest("[data-g]");
      if (!i) return;
      const r = await api("/api/vm/set_global", { name: i.dataset.g, value: i.value.trim() });
      if (r.snapshot) applySnap(r.snapshot, true);
    });

    // chart modes / zoom / edit
    $("#bc-mode").addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (b) setBcMode(b.dataset.mode);
    });
    $("#sizeview").addEventListener("click", (ev) => {
      const row = ev.target.closest(".sz-rt");
      if (!row) return;
      setBcMode("code");
      const ins = $(`#bytecode .ins[data-pc="${row.dataset.pc}"]`);
      const box = $("#bytecode");
      if (ins) box.scrollTop = Math.max(0, ins.offsetTop - box.offsetTop - 30);
    });
    $("#fc-mode").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-mode]");
      if (!b) return;
      chartMode = b.dataset.mode;
      $$("#fc-mode button").forEach((x) => x.classList.toggle("on", x === b));
      Edit.close();
      renderChart();
    });
    const setZoom = (z) => { zoom = Math.max(0.3, Math.min(2.5, z)); renderChart(); };
    $("#zoom-in").onclick = () => setZoom(zoom * 1.2);
    $("#zoom-out").onclick = () => setZoom(zoom / 1.2);
    $("#zoom-fit").onclick = () => setZoom(1);
    $("#chart-wrap").addEventListener("wheel", (ev) => { if (ev.ctrlKey || ev.metaKey) { ev.preventDefault(); setZoom(zoom * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)); } }, { passive: false });
    $("#edit-mode").onchange = () => { if (!$("#edit-mode").checked) Edit.close(); renderChart(); };

    // chart click when not editing: navigate / breakpoints / state diagram
    $("#chart").addEventListener("click", (ev) => {
      if ($("#edit-mode").checked) return;
      const cell = ev.target.closest("[data-goto-node]");
      if (cell) {                                  // state x event table: show the handler
        chartMode = "flow";
        $$("#fc-mode button").forEach((x) => x.classList.toggle("on", x.dataset.mode === "flow"));
        renderChart();
        const head = $(`#chart [data-head="${cell.dataset.gotoNode}"]`) || $(`#chart [data-sel="${cell.dataset.gotoNode}"]`);
        if (head) { scrollChartTo(head); head.classList.add("sel"); setTimeout(() => head.classList.remove("sel"), 1200); }
        return;
      }
      const st = ev.target.closest("[data-state]");
      if (chartMode === "states" && st) {
        chartMode = "flow";
        $$("#fc-mode button").forEach((x) => x.classList.toggle("on", x.dataset.mode === "flow"));
        renderChart();
        const box = $(`#chart .nd-state[data-state="${st.dataset.state}"] rect`);
        if (box) scrollChartTo(box);
        return;
      }
      const nd = ev.target.closest("[data-line]");
      if (!nd) return;
      const line = +nd.dataset.line;
      if (ev.altKey || ev.detail === 2) toggleBreak(line);
      else { scrollToLine(line); hoverLine(line, true); setTimeout(() => hoverLine(line, false), 900); }
    });
    $("#chart").addEventListener("mouseover", (ev) => {
      const nd = ev.target.closest("[data-ids]");
      if (!nd || !nd.dataset.line) return;
      hoverLine(+nd.dataset.line, true);
      const ids = nd.dataset.ids.split(" ");
      ids.forEach((id) => $$(`#bytecode .ins[data-node="${id}"]`).forEach((r) => r.classList.add("hov")));
    });
    $("#chart").addEventListener("mouseout", (ev) => {
      const nd = ev.target.closest("[data-ids]");
      if (!nd || !nd.dataset.line) return;
      hoverLine(+nd.dataset.line, false);
      $$("#bytecode .ins.hov").forEach((r) => r.classList.remove("hov"));
    });
    $("#bytecode").addEventListener("mouseover", (ev) => {
      const r = ev.target.closest(".ins");
      if (!r) return;
      hoverLine(+r.dataset.line, true);
      hoverNode([r.dataset.node, r.dataset.stmt], true);
    });
    $("#bytecode").addEventListener("mouseout", (ev) => {
      const r = ev.target.closest(".ins");
      if (!r) return;
      hoverLine(+r.dataset.line, false);
      hoverNode([r.dataset.node, r.dataset.stmt], false);
    });

    // keyboard
    document.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") { ev.preventDefault(); compile(); }
      else if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); $("#btn-save").click(); }
      else if (ev.key === "F5") { ev.preventDefault(); step("run"); }
      else if (ev.key === "F10") { ev.preventDefault(); step("over"); }
      else if (ev.key === "F11" && ev.shiftKey) { ev.preventDefault(); step("out"); }
      else if (ev.key === "F11") { ev.preventDefault(); step("stmt"); }
      else if (ev.key === "F7") { ev.preventDefault(); step("instr"); }
      else if (ev.key === "Escape") Edit.close();
    });

    // splitters
    $$(".splitter").forEach((sp) => {
      sp.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        const grid = $("#views");
        const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").map(parseFloat);
        const i = +sp.dataset.split * 2;
        const x0 = ev.clientX, a = cols[i], b = cols[i + 2];
        const move = (e) => {
          const d = Math.max(-a + 160, Math.min(b - 160, e.clientX - x0));
          const c = cols.slice();
          c[i] = a + d; c[i + 2] = b - d;
          grid.style.gridTemplateColumns = c.map((v, k) => (k % 2 ? "5px" : v + "px")).join(" ");
        };
        const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }

  const App = {
    api,
    ast: () => (data ? data.ast : null),
    blocks: () => blocks,
    editMode: () => $("#edit-mode").checked,
    fromModel,
    markSelected,
  };
  window.FlowApp = App;

  wire();
  Edit.init(App);
  api("/api/load").then((res) => applyCompile(res, false));
})();
