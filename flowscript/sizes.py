"""Size report: where the bytes of a compiled program go, and how much RAM the C VM needs.

Three separate budgets matter on a device:

* the **image** (.fsb) – the C VM uses it in place (never copied), so it lives wherever the
  host keeps the download, typically flash;
* the **RAM** of the ``fs_vm`` struct – fixed by the ``FS_*`` macros the firmware is built
  with, so the report computes the smallest settings this program can run with;
* the **C interpreter code** (flowscript.c) – fixed, independent of the program.
"""

from .bytecode import Program, decode, BUILTIN_EVENTS
from .values import BUILTINS

# C VM defaults (c/flowscript.h)
DEFAULTS = {
    "FS_STACK_SIZE": 64, "FS_MAX_FRAMES": 16, "FS_QUEUE_SIZE": 16, "FS_MAX_EXTERNS": 64,
    "FS_MAX_GLOBALS": 256, "FS_MAX_TIMERS": 16, "FS_MAX_EVENT_ARGS": 8,
    "FS_STR_SLOTS": 16, "FS_STR_LEN": 64,
}
VALUE = (16, 8)              # fs_value: type byte + 8-byte union (double/int64 align 8)
VM_CODE_BYTES = 6 * 1024     # flowscript.c, -Os Thumb-2 (approx., see README)

# net stack effect of instructions that just fall through (CALL/XCALL/EMIT/RET handled apart)
_EFFECT = {
    "NOP": 0, "PUSH": 1, "PUSH8": 1, "PUSHS": 1, "PUSHF": 1, "PUSHT": 1, "POP": -1, "DUP": 1,
    "LOADG": 1, "STOREG": -1, "LOADL": 1, "STOREL": -1,
    "NEG": 0, "BNOT": 0, "NOT": 0, "BOOL": 0, "TSTART": -1, "TEVERY": -1, "TSTOP": 0,
    "JMP": 0, "JZ": -1, "JNZ": -1,
}
for _op in ("ADD", "CONCAT", "SUB", "MUL", "DIV", "MOD", "BAND", "BOR", "BXOR", "SHL", "SHR",
            "EQ", "NE", "LT", "LE", "GT", "GE"):
    _EFFECT[_op] = -1


# ---------------------------------------------------------------------------------------
# C struct layout (mirrors struct fs_vm in c/flowscript.h)

def _layout(fields):
    """fields: (size, align) in declaration order -> (sizeof, alignof) with C padding."""
    off = 0
    max_align = 1
    for size, align in fields:
        off = (off + align - 1) // align * align
        off += size
        max_align = max(max_align, align)
    return (off + max_align - 1) // max_align * max_align, max_align


def fs_vm_size(cfg, ptr=4):
    """sizeof(fs_vm) for the given FS_* settings; ptr = pointer size (4 on Cortex-M)."""
    P = (ptr, ptr)
    i32 = (4, 4)
    u16 = (2, 2)
    u8 = (1, 1)
    qentry, qalign = _layout([u16, (VALUE[0] * cfg["FS_MAX_EVENT_ARGS"], VALUE[1])])
    timer, talign = _layout([u8, u8, (2, 2), i32, (8, 8)])
    strslot, salign = _layout([u16, u8, u8, (cfg["FS_STR_LEN"] + 1, 1)])
    fields = [P, P] + [P] * 9 + [i32] + [u16] * 10 + [u8]
    fields += [(ptr * cfg["FS_MAX_EXTERNS"], ptr), P, P, P, P, P]  # bound, on_emit/fault/timer, clock, user
    fields += [(VALUE[0] * cfg["FS_MAX_GLOBALS"], VALUE[1]), (VALUE[0] * cfg["FS_STACK_SIZE"], VALUE[1]), i32,
               (8 * cfg["FS_MAX_FRAMES"], 4)]
    fields += [i32] * 7 + [u8, u8] + [i32] * 4
    fields += [(qentry * cfg["FS_QUEUE_SIZE"], qalign), i32, i32,
               (timer * cfg["FS_MAX_TIMERS"], talign), (strslot * cfg["FS_STR_SLOTS"], salign)]
    return _layout(fields)[0]


def makes_strings(prog):
    """Can this program create run-time strings (so it needs the string slots)?
    Yes if it joins strings (CONCAT), calls str() / substr(), has an extern returning
    a str or an event with a str parameter (the host's strings are copied in)."""
    from .values import STR
    if any(e[3] == STR for e in prog.externs) or any(STR in e[2] for e in prog.events):
        return True
    for pc in _pcs(prog):
        op, arg, _size = decode(prog.code, pc)
        if op == "CONCAT" or (op == "BUILTIN" and arg in (5, 6, 7, 22)):
            return True
    return False


def _pcs(prog):
    pc = 0
    while pc < len(prog.code):
        yield pc
        pc += decode(prog.code, pc)[2]


# ---------------------------------------------------------------------------------------
# bytecode analysis

def _routines(prog):
    """[(kind, name, argc, nlocals, start, end)] in code order."""
    out = []
    for name, argc, nlocals, off in prog.funcs:
        out.append(("func", name, argc, nlocals, off))
    for st, ev, argc, nlocals, off in prog.handlers:
        evn = prog.events[ev][0]
        name = ("on %s" % evn) if st is None else ("%s: on %s" % (prog.states[st], evn))
        out.append(("handler", name, argc, nlocals, off))
    out.sort(key=lambda r: r[4])
    res = []
    for i, r in enumerate(out):
        end = out[i + 1][4] if i + 1 < len(out) else len(prog.code)
        res.append(r + (max(end, r[4]),))
    return res


def _local_peak(prog, start, nlocals):
    """Walk one routine. Returns (peak depth relative to its base, call sites).

    call sites: [(depth_before_call, func_index)].
    """
    code = prog.code
    seen = {}
    work = [(start, nlocals)]
    peak = nlocals
    calls = []
    while work:
        pc, d = work.pop()
        while 0 <= pc < len(code):
            if seen.get(pc, -1) >= d:
                break
            seen[pc] = d
            name, arg, size = decode(code, pc)
            npc = pc + size
            if name in ("RET", "RET0", "GOTO", "???"):
                break
            if name == "CALL":
                if arg >= len(prog.funcs):
                    break
                calls.append((d, arg))
                d = d - prog.funcs[arg][1] + 1
            elif name == "XCALL":
                if arg >= len(prog.externs):
                    break
                d = d - prog.externs[arg][1] + 1
            elif name == "BUILTIN":
                if arg >= len(BUILTINS):
                    break
                d = d - len(BUILTINS[arg][1]) + 1
            elif name == "EMIT":
                if arg >= len(prog.events):
                    break
                d -= prog.events[arg][1]
            else:
                d += _EFFECT.get(name, 0)
            peak = max(peak, d)
            if name in ("JZ", "JNZ"):
                work.append((arg, d))
            elif name == "JMP":
                npc = arg
            pc = npc
    return peak, calls


def stack_analysis(prog):
    """Worst-case stack slots and call frames per routine (None = unbounded: recursion)."""
    funcs = {}
    for i, (name, argc, nlocals, off) in enumerate(prog.funcs):
        funcs[i] = _local_peak(prog, off, nlocals)
    memo = {}

    def total(i, path=()):
        """(stack peak relative to the func's base, frames incl. its own) or None."""
        if i in path:
            return None
        if i in memo:
            return memo[i]
        peak, calls = funcs[i]
        frames = 1
        for d, callee in calls:
            t = total(callee, path + (i,))
            if t is None:
                memo[i] = None
                return None
            peak = max(peak, d - prog.funcs[callee][1] + t[0])
            frames = max(frames, 1 + t[1])
        memo[i] = (peak, frames)
        return memo[i]

    result = []
    for kind, name, argc, nlocals, start, end in _routines(prog):
        if kind == "func":
            idx = [i for i, f in enumerate(prog.funcs) if f[3] == start and f[0] == name][0]
            t = total(idx)
        else:
            peak, calls = _local_peak(prog, start, nlocals)
            frames = 1
            t = (peak, frames)
            for d, callee in calls:
                c = total(callee)
                if c is None:
                    t = None
                    break
                t = (max(t[0], d - prog.funcs[callee][1] + c[0]), max(t[1], 1 + c[1]))
        result.append({"kind": kind, "name": name, "stack": t[0] if t else None,
                       "frames": t[1] if t else None})
    return result


def _usage(prog):
    used = {"XCALL": set(), "CALL": set(), "GOTO": set(), "EMIT": set(), "PUSHS": set(),
            "LOADG": set(), "STOREG": set(), "TIMER": set()}
    pc = 0
    while pc < len(prog.code):
        name, arg, size = decode(prog.code, pc)
        if name in used:
            used[name].add(arg)
        elif name in ("TSTART", "TEVERY"):
            used["TIMER"].add(arg)
        pc += size
    return used


# ---------------------------------------------------------------------------------------

def _sbytes(s):
    return 2 + len(s.encode("utf-8", "surrogateescape")) + 1


def size_report(prog, debug=False, image=None):
    """Everything as a JSON-ready dict (used by the CLI and the debugger).

    image: the original .fsb bytes when reporting on an existing file (prog loaded from it).
    """
    if image is not None:
        debug = bool(Program.from_bytes(image).lines) or debug
    else:
        image = prog.to_bytes(debug=debug)
    full = Program.from_bytes(image)          # string table as it is really written
    strings = full.strings

    sections = [("header", 32, "magic, version, counts")]
    sections.append(("strings", sum(_sbytes(s) for s in strings),
                     "%d strings (2-byte length + text + NUL each)" % len(strings)))
    sections.append(("globals", 9 * len(prog.globals), "%d initial values x 9 (type + 8 bytes)" % len(prog.globals)))
    sections.append(("externs", 8 * len(prog.externs), "%d x 8 (name, argc, return and parameter types)" % len(prog.externs)))
    sections.append(("events", 6 * len(prog.events),
                     "%d x 6 (incl. built-in boot/enter/exit)" % len(prog.events)))
    sections.append(("states", 2 * len(prog.states), "%d x 2" % len(prog.states)))
    sections.append(("timers", 2 * len(prog.timers), "%d x 2" % len(prog.timers)))
    sections.append(("funcs", 6 * len(prog.funcs), "%d x 6" % len(prog.funcs)))
    sections.append(("handlers", 8 * len(prog.handlers), "%d x 8" % len(prog.handlers)))
    sections.append(("code", len(prog.code), "bytecode"))
    dbg_bytes = (len(prog.to_bytes(debug=True)) - len(prog.to_bytes(debug=False))) if prog.lines else None
    if debug:
        sections.append(("debug", len(image) - sum(s[1] for s in sections),
                         "line table + global-name indices (the names are in strings)"))
    assert sum(s[1] for s in sections) == len(image), "size report out of sync with to_bytes"

    # what each string is used for
    roles = {i: [] for i in range(len(strings))}
    idx = {s: i for i, s in enumerate(strings)}
    if prog.name:
        roles[idx[prog.name]].append("program name")
    for n, *_rest in prog.externs:
        roles[idx[n]].append("extern")
    for i, (n, *_rest) in enumerate(prog.events):
        roles[idx[n]].append("built-in event" if i < len(BUILTIN_EVENTS) else "event")
    for n in prog.states:
        roles[idx[n]].append("state")
    for f in prog.funcs:
        roles[idx[f[0]]].append("func")
    used = _usage(prog)
    for i, s in enumerate(prog.strings):
        if i in used["PUSHS"]:
            roles[idx[s]].append("literal")
    if debug:
        for n in prog.global_names:
            if n in idx:
                roles[idx[n]].append("global name (debug)")
    string_rows = sorted(({"text": s, "bytes": _sbytes(s), "roles": roles[i]}
                          for i, s in enumerate(strings)), key=lambda r: -r["bytes"])
    by_role = {}
    for r in string_rows:
        key = r["roles"][0] if r["roles"] else "unused"
        by_role[key] = by_role.get(key, 0) + r["bytes"]

    routines = [{"kind": k, "name": n, "start": s, "bytes": e - s}
                for k, n, _a, _l, s, e in _routines(prog)]
    stacks = {r["name"]: r for r in stack_analysis(prog)}
    for r in routines:
        r["stack"] = stacks[r["name"]]["stack"]
        r["frames"] = stacks[r["name"]]["frames"]

    # RAM: smallest FS_* settings that run this program
    handler_rows = [r for r in routines if r["kind"] == "handler"]
    unbounded = [r["name"] for r in handler_rows if r["stack"] is None]
    deep_stack = max(handler_rows, key=lambda r: r["stack"] or 0) if handler_rows else None
    deep_frames = max(handler_rows, key=lambda r: r["frames"] or 0) if handler_rows else None
    minimum = dict(DEFAULTS)
    why = {}
    shown = dict(minimum)
    shown["FS_QUEUE_SIZE"] = None
    if not unbounded and handler_rows:
        minimum["FS_STACK_SIZE"] = max(1, deep_stack["stack"])
        why["FS_STACK_SIZE"] = "deepest: %s" % deep_stack["name"]
        minimum["FS_MAX_FRAMES"] = max(1, deep_frames["frames"])
        why["FS_MAX_FRAMES"] = "deepest call chain: %s" % deep_frames["name"]
    elif unbounded:
        why["FS_STACK_SIZE"] = why["FS_MAX_FRAMES"] = "recursion (%s): no static bound" % unbounded[0]
        shown["FS_STACK_SIZE"] = shown["FS_MAX_FRAMES"] = None
    minimum["FS_MAX_GLOBALS"] = max(1, len(prog.globals))
    minimum["FS_MAX_EXTERNS"] = max(1, len(prog.externs))
    minimum["FS_MAX_TIMERS"] = max(1, len(prog.timers))
    minimum["FS_MAX_EVENT_ARGS"] = max([1] + [e[1] for e in prog.events])
    if makes_strings(prog):
        why["FS_STR_SLOTS"] = why["FS_STR_LEN"] = "the program makes strings: size for its data"
    else:
        minimum["FS_STR_SLOTS"] = minimum["FS_STR_LEN"] = 1
        why["FS_STR_SLOTS"] = why["FS_STR_LEN"] = "the program never makes a string at run time"
    for k in ("FS_STACK_SIZE", "FS_MAX_FRAMES", "FS_MAX_GLOBALS", "FS_MAX_EXTERNS",
              "FS_MAX_TIMERS", "FS_MAX_EVENT_ARGS", "FS_STR_SLOTS", "FS_STR_LEN"):
        if not (unbounded and k in ("FS_STACK_SIZE", "FS_MAX_FRAMES")):
            shown[k] = minimum[k]
    why["FS_QUEUE_SIZE"] = "depends on how many events the host posts between runs"
    ram = {
        "rows": [{"macro": k, "default": DEFAULTS[k], "minimum": shown[k], "why": why.get(k, "")}
                 for k in ("FS_STACK_SIZE", "FS_MAX_FRAMES", "FS_MAX_GLOBALS", "FS_MAX_EXTERNS",
                           "FS_MAX_TIMERS", "FS_MAX_EVENT_ARGS", "FS_STR_SLOTS", "FS_STR_LEN",
                           "FS_QUEUE_SIZE")],
        "default_bytes": fs_vm_size(DEFAULTS),
        "minimum_bytes": fs_vm_size(minimum),
        "queue_entry_bytes": _layout([(2, 2), (VALUE[0] * minimum["FS_MAX_EVENT_ARGS"], VALUE[1])])[0],
        "cflags": " ".join("-D%s=%d" % (k, shown[k]) for k in sorted(shown)
                           if shown[k] is not None and shown[k] != DEFAULTS[k]),
        "unbounded": unbounded,
    }

    # hints
    hints = []
    names = lambda pairs: [n for n, _a in pairs]
    for i, (n, *_rest) in enumerate(prog.externs):
        if i not in used["XCALL"]:
            hints.append("extern '%s' is never called: removing it saves %d bytes "
                         "(and the firmware need not bind it)" % (n, 4 + _sbytes(n)))
    for i, f in enumerate(prog.funcs):
        if i not in used["CALL"]:
            r = [x for x in routines if x["kind"] == "func" and x["name"] == f[0]]
            hints.append("func '%s' is never called: removing it saves %d bytes"
                         % (f[0], 6 + _sbytes(f[0]) + (r[0]["bytes"] if r else 0)))
    handled = {ev for _s, ev, _a, _l, _o in prog.handlers}
    for i, (n, *_rest) in enumerate(prog.events):
        if i < len(BUILTIN_EVENTS) or i in prog.timers:
            continue
        if i not in handled and i not in used["EMIT"]:
            hints.append("event '%s' is neither handled nor emitted: removing it saves %d bytes"
                         % (n, 4 + _sbytes(n)))
    for ti, ev in enumerate(prog.timers):
        if ti not in used["TIMER"]:
            hints.append("timer '%s' is never started" % prog.events[ev][0])
    gnames = prog.global_names or ["#%d" % i for i in range(len(prog.globals))]
    for i, n in enumerate(gnames):
        if i not in used["LOADG"]:
            hints.append("global '%s' is never read: removing it saves 4 bytes of image and RAM, "
                         "plus its stores" % n)
        elif i not in used["STOREG"]:
            hints.append("global '%s' is never assigned: a const would save 4 bytes of image and "
                         "RAM and 1 byte per use" % n)
    for i, n in enumerate(prog.states):
        if i and i not in used["GOTO"]:
            hints.append("state '%s' is never entered (no goto_state to it)" % n)
    NAME_ROLES = {"extern", "event", "built-in event", "state", "func", "program name"}
    name_bytes = sum(r["bytes"] for r in string_rows if NAME_ROLES & set(r["roles"]))
    if debug:
        dbg_only = sum(r["bytes"] for r in string_rows if r["roles"] == ["global name (debug)"])
        dbg_bytes = [x[1] for x in sections if x[0] == "debug"][0] + dbg_only
    if name_bytes:
        hints.append("names (externs, events, states, funcs) take %d bytes; they are stored for "
                     "binding by name and for fs_state_name(), so shorter names give a smaller "
                     "image" % name_bytes)
    if debug and dbg_bytes:
        hints.append("the debug section adds %d bytes: compile without -g for the device" % dbg_bytes)
    if unbounded:
        hints.append("recursion in %s: the stack/frame need cannot be computed; a fault "
                     "(stack overflow / call depth) stops it at run time" % ", ".join(unbounded))
    hints.append("RAM settings marked 'minimum' are also load-time limits: fs_load rejects later "
                 "downloads that need more, so size them for the largest program you plan to ship")

    return {
        "name": prog.name, "image_bytes": len(image), "debug": debug,
        "debug_bytes": dbg_bytes,
        "sections": [{"name": n, "bytes": b, "detail": d} for n, b, d in sections],
        "routines": routines, "strings": string_rows, "strings_by_role": by_role,
        "ram": ram, "vm_code_bytes": VM_CODE_BYTES, "hints": hints,
    }


def _bar(n, total, width=24):
    if not total:
        return ""
    k = int(round(width * n / float(total)))
    return "#" * k


def format_report(rep, title=""):
    out = []
    w = out.append
    total = rep["image_bytes"]
    w("FlowScript size report%s" % ((" — " + title) if title else ""))
    w("")
    w("IMAGE  %d bytes  (.fsb; the C VM reads it in place, e.g. from flash)%s"
      % (total, "  [+%d with -g]" % rep["debug_bytes"] if rep["debug_bytes"] and not rep["debug"] else ""))
    for s in rep["sections"]:
        if not s["bytes"]:
            continue
        w("  %-9s %6d  %3d%%  %-24s %s" % (s["name"], s["bytes"], round(100.0 * s["bytes"] / total),
                                          _bar(s["bytes"], total), s["detail"]))
    w("")
    code = sum(r["bytes"] for r in rep["routines"])
    w("CODE BY ROUTINE  %d bytes" % code)
    w("  %-30s %6s  %5s  %6s" % ("", "bytes", "stack", "frames"))
    for r in sorted(rep["routines"], key=lambda r: -r["bytes"]):
        st = "-" if r["stack"] is None else str(r["stack"])
        fr = "-" if r["frames"] is None else str(r["frames"])
        w("  %-30s %6d  %5s  %6s  %s" % (("func " if r["kind"] == "func" else "") + r["name"],
                                         r["bytes"], st, fr, _bar(r["bytes"], code, 20)))
    w("")
    w("STRINGS  %d bytes  (%s)" % (sum(r["bytes"] for r in rep["strings"]),
                                    ", ".join("%s %d" % kv for kv in sorted(
                                        rep["strings_by_role"].items(), key=lambda kv: -kv[1]))))
    for r in rep["strings"][:12]:
        w("  %4d  %-32s %s" % (r["bytes"], '"%s"' % r["text"], ", ".join(r["roles"])))
    if len(rep["strings"]) > 12:
        w("  ... %d more" % (len(rep["strings"]) - 12))
    w("")
    ram = rep["ram"]
    w("RAM  sizeof(fs_vm) on a 32-bit target: %d bytes with the defaults, %d bytes with the "
      "minimum settings" % (ram["default_bytes"], ram["minimum_bytes"]))
    w("  %-18s %8s %8s" % ("", "default", "minimum"))
    for r in ram["rows"]:
        w("  %-18s %8d %8s  %s" % (r["macro"], r["default"],
                                   "-" if r["minimum"] is None else r["minimum"], r["why"]))
    w("  each queue entry costs %d bytes" % ram["queue_entry_bytes"])
    if ram["cflags"]:
        w("  build flags: %s" % ram["cflags"])
    w("")
    w("C VM CODE  ~%d KB (flowscript.c), fixed, independent of the program" % (rep["vm_code_bytes"] // 1024))
    if rep["hints"]:
        w("")
        w("HINTS")
        for h in rep["hints"]:
            w("  - " + h)
    return "\n".join(out) + "\n"
