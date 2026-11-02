# FlowScript

FlowScript is a small event-driven language for the **flow control** of a bigger system.
The system software (C firmware, a Python service, …) stays fixed and stable; the logic
is written in FlowScript, compiled to compact bytecode and downloaded to the device.

The language is designed so that **every statement is one flowchart node** and **every
condition is a logic-gate circuit**. Script and flowchart are two views of the same
model, so you can edit either one.

```
 ┌────────── source (.fs) ──────────┐       ┌───────── flowchart ─────────┐
 │ state driving:                   │       │      ⬡ on report             │
 │   on report:                     │ parse │            │                 │
 │     if speed > LIMIT and not low:│ ────▶ │  [speed>LIMIT]─┐             │
 │       send_position(2)           │ ◀──── │  [low]─NOT─────AND──▶ ◇ yes  │
 │                                  │ format│            ▱ send_position(2)│
 └──────────────────────────────────┘       └──────────────────────────────┘
                 │ compile
                 ▼
 bytecode (.fsb) ──▶  C VM (firmware)   flowscript.c, no malloc, ~16 KB Thumb-2
                 └─▶  Python VM          reference implementation + debugger
```

## Quick start

```sh
python3 -m flowscript debug examples/gps_tracker.fs     # 3-view debugger in the browser
python3 -m flowscript compile examples/gps_tracker.fs   # -> examples/gps_tracker.fsb
python3 -m flowscript disasm examples/gps_tracker.fsb
python3 -m flowscript run examples/gps_tracker.fs examples/gps_tracker.scenario
python3 -m flowscript fmt examples/gps_tracker.fs       # canonical formatting
python3 -m flowscript mermaid examples/gps_tracker.fs   # Mermaid flowchart export
python3 -m flowscript size examples/gps_tracker.fs      # where the bytes go + RAM needed
python3 -m flowscript ast examples/gps_tracker.fs       # the visual model as JSON

cd c && make && ./fsrun ../examples/gps_tracker.fsb ../examples/gps_tracker.scenario
```

Requirements: Python 3.9+ (stdlib only), any C99 compiler. No other dependencies.

## Using FlowScript from Python

```sh
pip install flowscript-vm          # imports as `flowscript`; adds the `flowscript` command
```

A Python service can compile scripts, run them (from source or bytecode) and draw them:

```python
import time
import flowscript

program = flowscript.compile(source)          # flowscript.CompileError(.line, .col) on errors
for w in program.warnings:                     # compiles, but probably not as intended
    print(w.line, w.message)

machine = flowscript.Machine(
    program,                                   # or bytecode: flowscript.load(path_or_bytes)
    externs={"param": settings.__getitem__,    # one Python callable per `extern`
             "diag": diagnostics.__setitem__},
    clock=time.time,                           # what now() returns (seconds, or a datetime)
    on_event=lambda name, args: ...,           # every raise_event
    on_log=lambda level, message, template, values: log.log(LEVELS[level], message),
                                               # every log statement (warning "x is {x}")
    on_fault=lambda fault: log.warning(fault), # also kept in machine.faults
)
machine.start()                                # on boot, then enter the initial state
machine.send("tick")                           # post an event and run until idle
machine.advance(1000)                          # let timers run for 1 s (or run them yourself:
                                               #   on_timer=... and machine.timer_expired(name))
machine.state                                  # "trading"
machine["aggression"], machine.globals         # variables by name

data = program.to_bytes()                      # the .fsb image the C VM runs; program.save(path)
```

Values cross the boundary as plain Python: `int`, `float`, `str` and timezone-aware
`datetime` for FlowScript's `time`. The Python VM is the reference implementation: a
program behaves exactly as on the C VM, including faults (a faulting handler is aborted
and reported; the machine carries on).

### Upgrading scripts from 0.1

0.2 dropped `any_state`: only states handle events, `on boot` lives in the initial state,
and functions can return several results instead. `flowscript.migrate(source)` (or
`flowscript migrate FILE.fs [-w]`) rewrites a 0.1 program: `on boot` moves into the
initial state and each shared handler is copied into the states that didn't override it.
It returns notes on what may behave differently, chiefly timers started from
`any_state`, which now stop when their state is left. Bytecode is format version 3:
recompile `.fsb` files.

### Flowcharts as SVG

```python
svg = flowscript.to_svg(program)                          # the flowchart
svg = flowscript.to_svg(program, view="states")           # the state diagram
svg = flowscript.to_svg(program, state=machine.state,     # highlight the live state
                        width="100%")                     # scale to the container
```

The result is a self-contained `<svg>` string: inline it in a page (in a Django template:
`{{ svg|safe }}`) or save it as a file. It has no scripts and no external references.
Its colours are CSS variables (`--fs-bg`, `--fs-fg`, `--fs-accent`, ... see
`flowscript.svg.THEMES`) that the page may override, and `theme="auto"` (the default)
follows `prefers-color-scheme` and a `data-theme="light"`/`"dark"` attribute on an
ancestor such as `<html>`; `theme="dark"`/`"light"` fix it. Nodes carry `data-line` (the
source line) and states `data-state`, for pages that add their own interaction.

## The language in one page

```flowscript
flow tracker version 3          # name + version are stored in the bytecode

extern gps_speed() -> int       # functions the host provides (bound by name at load)
extern send_position(int reason, str note)   # no '-> type': returns nothing
event ignition(int active)      # events the host raises (up to 8 typed args)
timer report                    # VM-managed timer; also an event called "report"
const LIMIT = 90                # compile-time constant (any type)
int reports = 0                 # global variables: int, float, str, time
time since = time(2026, 1, 1)

func notify(int reason, int count) -> int, int:   # sub-process: sees params, consts, externs, not globals
  send_position(reason, "report " + str(count))
  return count + 1, reason      # several results go back to the handler

initial state parked:           # the program starts here (required with 2+ states)
  on boot:                      # runs once at start, before parked is entered
    since = now()               # the host's clock

  on enter:
    start report every 30s      # time units: ms, s, min, h

  on ignition(active):
    if active:
      goto_state driving        # ends the handler; runs exit/enter handlers

state driving:                  # only states handle events: a state lists all it reacts to
  int slow_reports = 0          # state variable: only visible here, reset on each entry
  int last_reason = 0

  on enter:
    start report every 30s      # same period: the timer keeps running across the transition

  on report:
    float kmh = gps_speed() * 1.0   # local variable; ints promote to float
    if kmh > LIMIT and not reports > 100:
      warning "speeding: {kmh} km/h, limit {LIMIT}"   # a log message: the host formats it
      reports, last_reason = notify(2, reports)   # several results, assigned in order
    elif kmh == 0 and now() - since > 10min:
      raise_event ignition(0)   # queued for the script + host notified
    else:
      slow_reports = slow_reports + 1
      reports, last_reason = notify(1, reports)
```

Blocks are defined by indentation, as in Python: a header line ends with `:` and its
body is indented below it (the formatter uses 2 spaces; tabs are rejected). `pass`
marks an empty block. One statement per line, so every line is one flowchart node.

Four types — `int` (int32), `float` (double), `str`, `time` (UTC, ms) — checked at compile
time, with int → float promotion and nothing else implicit; built-in conversions
(`int() float() str() time()`), string (`len substr find`), time (`now date year … weekday`)
and math functions (`abs min max floor ceil round sqrt exp log pow`). Statements:
`if/elif/else`, `while`, `goto_state`, `raise_event`, `start/stop`, `return`, log messages
(`warning "x is {x}"`: the host gets a template and the values, so they work in every build), functions
(with several results: `a, b = f(x)`),
and the operators `or xor and not == != < <= > >= | ^ & << >> + - * / % ~`. That is the
whole language. See [SPEC.md](https://github.com/RubenNorgaard/flowscript/blob/main/SPEC.md) for the exact semantics and the bytecode format.

Strings made at run time live in a fixed pool of slots inside the VM (`FS_STR_SLOTS` ×
`FS_STR_LEN` bytes, default 16 × 64), reclaimed by a mark-and-sweep pass when the pool is
full: no heap, and running out is a fault like any other. `examples/pct_pid.fs` is a
larger program (a PID-steered market-making controller) using floats and times.

| Statement | Flowchart symbol |
|---|---|
| `on EVENT` | event (hexagon) |
| `state S` | state container (each state holds every handler it runs) |
| `x = …`, `var x = …` | process (rectangle) |
| state `var` | "state variables" card inside the state container |
| `extern_fn(…)` | I/O (parallelogram) |
| `func_name(…)` | predefined process (double-sided rectangle) |
| `if` / `elif` / `else` | decision (diamond) fed by a logic-gate tree |
| `while` | decision with a back edge |
| `and` `or` `xor` `not` | AND / OR / XOR / NOT gates |
| `goto_state S` | state transition (red terminator) |
| `raise_event E` | signal (flag) |
| `warning "x is {x}"` (`debug` `info` `warning` `error`) | I/O, outlined by level: a log message for the host |
| `start` / `stop` | timer (hourglass) |
| `return` | terminator |
| `# comment` | annotation (attached to the node below it) |

## The debugger (3 views)

`python3 -m flowscript debug FILE.fs` opens a browser page with

1. **FlowScript source** – editable, syntax highlighted, click the gutter for breakpoints.
2. **Bytecode** – disassembly with the current instruction and the instructions of the
   current statement highlighted.
3. **Flowchart** – the executing node is highlighted; conditions are drawn as logic
   gates that turn green/red as their inputs are evaluated. A *States* tab shows the
   state machine derived from the `goto_state`s.

Hovering in any view highlights the corresponding parts of the other two.
Stepping: *Step instr* (F7), *Step node* (F11), *Step over* (F10), *Step out*
(Shift+F11), *Finish* handler, *Run* to idle/breakpoint (F5). Post events with
arguments, advance time with *Tick*, set the return values of the host functions
(“host stubs”), and edit globals live.

**Visual editing:** tick *edit* in the flowchart pane. Click any node to edit it in the
inspector, use the `+` handles on the edges to insert nodes, wrap any condition input in
`NOT` / `AND` / `OR` / `XOR`, add handlers, functions, states and declarations. Every
change regenerates the script (and recompiles), so the two views never diverge.

## Embedding the C VM

```c
#include "flowscript.h"

static fs_vm vm;                                    /* static, no heap */

static fs_value x_gps_speed(fs_vm *vm, const fs_value *a, int n, void *u) { return fs_int(gps_speed_kmh()); }
static fs_value x_send(fs_vm *vm, const fs_value *a, int n, void *u) {
    modem_send(a[0].as.i, fs_str(vm, a[1], NULL));  /* a str argument: valid during the call */
    return fs_int(0);                               /* ignored: send_position returns nothing */
}
static int64_t utc_ms(fs_vm *vm, void *u) { return rtc_unix_ms(); }   /* now() */

void logic_init(const uint8_t *image, size_t len) {
    if (fs_load(&vm, image, len) != FS_OK) { /* reject download, keep old logic */ }
    fs_bind(&vm, "gps_speed", x_gps_speed);
    fs_bind(&vm, "send_position", x_send);
    fs_set_clock(&vm, utc_ms);
    /* ... bind every extern; fs_unbound(&vm) names any you forgot ... */
    fs_start(&vm);
    fs_run(&vm, 100000);
}

void on_ignition(int on) {                         /* firmware event -> script */
    fs_value arg = fs_int(on);                      /* fs_float(), fs_time(), fs_cstr("text") */
    fs_post(&vm, fs_event_id(&vm, "ignition"), &arg, 1);
    fs_run(&vm, 100000);
}

void every_10ms(void) { fs_tick(&vm, 10); fs_run(&vm, 100000); }
```

`fs_tick` is the simple option. For low-power firmware, let the hardware run the
timers instead, so the CPU sleeps until something is actually due:

```c
static void on_timer(fs_vm *vm, int timer, int start, int32_t ms, int periodic, void *u) {
    if (start) hw_timer_arm(timer, ms, periodic);   /* RTC alarm, LPTIM, RTOS timer, ... */
    else       hw_timer_cancel(timer);
}

void main_loop(void) {                              /* or an RTOS task */
    for (;;) {
        int timer;
        sleep_until_interrupt();                    /* timer ISRs only record what fired */
        while (hw_timer_pop_expired(&timer))
            fs_timer_expired(&vm, timer);           /* ignored if the script stopped it */
        fs_run(&vm, 100000);
    }
}

/* in logic_init, before fs_start(): */
fs_set_timer_handler(&vm, on_timer);
```

Log statements (`warning "battery at {mv} mV"`) reach the host as a template and typed
values; the VM never builds the text, so they work in the smallest build. Render them, or
ship the template's position in the image and the raw values to be formatted elsewhere:

```c
static void on_log(fs_vm *vm, int level, const char *tmpl, uint16_t len,
                   const fs_value *v, int n, void *u) {
    char line[128];
    fs_format_log(vm, tmpl, len, v, n, line, sizeof line);   /* "battery at 3412 mV" */
    uart_printf("[%d] %s\n", level, line);
}

/* in logic_init: */
fs_set_log_handler(&vm, on_log);
```

Like every VM call, `fs_timer_expired` and `fs_run` must not interrupt a running
`fs_run`, so call them from one task or the main loop, not directly from an ISR.
A timer belongs to the state whose handler started it and stops automatically when
that state is left; in this mode the VM then calls `on_timer(..., start = 0, ...)`.

See [`c/flowscript.h`](https://github.com/RubenNorgaard/flowscript/blob/main/c/flowscript.h) for the full API and `c/fsrun.c` for a complete
host. The loader validates the whole image and every run-time operand, faults abort only
the current handler, each handler has an instruction budget, and a script that keeps
itself busy forever (e.g. two states whose `enter` handlers `goto_state` each other) is stopped
after 256 handler runs in a row. A bad download can therefore never take down the
firmware.

## Repository layout

```
SPEC.md                 language + bytecode specification (normative)
pyproject.toml          the Python package (pip install flowscript-vm)
flowscript/             Python toolchain
  api.py                host API: compile(), load(), Machine
  svg.py                SVG flowcharts and state diagrams (to_svg)
  lexer.py parser.py    source -> AST (= visual model, JSON)
  printer.py            AST -> canonical source
  compiler.py           AST -> bytecode + debug info
  bytecode.py           .fsb format, opcodes, disassembler
  vm.py                 reference VM
  runner.py             scenario runner (canonical trace format)
  mermaid.py            Mermaid export
  debugger/             3-view web debugger (server + static UI)
c/                      C VM (flowscript.c/.h), fsrun CLI, Makefile
examples/               example programs + scenarios
tests/                  unit tests, round-trip tests, C/Python conformance, fuzzing
```

## Tests

```sh
python3 -m unittest discover -s tests     # compiler, VM, round trip
cd c && make test                         # C API test + C/Python conformance (normal, ASan/UBSan,
                                          #   and the reduced FS_NO_* builds)
cd c && make fuzz                         # corrupt-image fuzzing against the ASan build
python3 tests/fuzz_c.py --runner fsrun-min  # ... or against a reduced build
python3 c/codesize.py                     # VM code size for every switch combination
cd c && make size                         # code size of the VM
```

C VM footprint (default sizes): **about 16 KB** of code for Cortex-M4 (`-Os`, Thumb-2,
what a `--gc-sections` link keeps, measured with `c/codesize.py`; v1, ints only, was
6.6 KB), plus the C library's double-precision routines (soft-float on a Cortex-M4) and
`floor ceil sqrt exp log pow fmod`; no data/bss of its own; a `fs_vm` struct of 6,080
bytes RAM on a 32-bit target with the default `FS_*` macros in `flowscript.h`. Most of
that is headroom: see below. Build with `-ffp-contract=off` (fused multiply-add would
change float results) and link `-lm`.

Inside the VM a value is stored as a payload cell (8 bytes: a double or an int64 time
fits) plus a 1-byte type tag kept in a separate array, so each global, stack slot and
queued event argument costs 9 bytes rather than the 16 a tagged struct would take after
padding. Hosts never see this: the API passes `fs_value` structs.

### Feature switches

Define these when building the C VM to leave out what your programs never use:

| Switch | Leaves out | Still works |
|---|---|---|
| `FS_NO_FLOAT` | `float` values, the float built-ins, soft-float and libm | everything else; no `-lm` needed |
| `FS_NO_TIME` | `time` values, `now()` and the date built-ins | `start t every 30s` (timers are not times) |
| `FS_NO_STR` | strings made at run time: `+` on strings, `str()`, `substr()`, `str` results and event arguments from the host, the string slots | string literals, e.g. `log("started", 3)`, `len()` and `find()` on them |
| `FS_NO_TEXT` | text conversion of floats and times in scripts: `str(float)`, `str(time)`, `float("...")`, `time("...")` | both types and all their arithmetic; `int` to and from text |

The compiler records in the image which of these a program needs, and `fs_load()`
refuses an image that needs a left-out feature with `FS_ERR_FEATURE`, so a mismatch
is caught at download, not in the field. `flowscript size` lists the switches a
program allows and includes them in its suggested build flags.

`fs_format()`, `fs_parse_float()` and `fs_parse_time()` stay available to the host in
every build (the test runner prints its traces with them). A device that never calls
them should link with `-ffunction-sections -Wl,--gc-sections`, so they and, with
`FS_NO_TEXT`, all the text code are dropped; the figures below assume that.

| Switches | VM code (Cortex-M4 `-Os`) | RAM per stored value |
|---|---|---|
| none | 15.7 KB + soft-float and libm | 9 bytes |
| `FS_NO_STR` | 13.5 KB + soft-float and libm | 9 bytes |
| `FS_NO_TEXT` | 12.5 KB + soft-float and libm | 9 bytes |
| `FS_NO_STR FS_NO_TEXT` (e.g. `pct_pid`) | 11.5 KB + soft-float and libm | 9 bytes |
| `FS_NO_TIME` | 12.5 KB + soft-float and libm | 9 bytes |
| `FS_NO_FLOAT` | 13.2 KB | 9 bytes |
| `FS_NO_FLOAT FS_NO_TIME` | 9.2 KB | 5 bytes |
| `FS_NO_FLOAT FS_NO_TIME FS_NO_STR` (e.g. the GPS tracker) | **8.2 KB**, needing only `memcpy memset memcmp strlen` | 5 bytes |

`python3 c/codesize.py` prints all 16 combinations. Every combination compiles
warning-free (`make switches`), and the conformance test runs each program on four
reduced builds, checking identical traces where the program is allowed and a refusal
where it is not.

## Optimizing size

`python3 -m flowscript size FILE.fs` (or `compile --size`, or the **Size** tab in the
debugger's bytecode pane) shows the three budgets separately:

* **Image** (the `.fsb`): bytes per section, code per handler/function, and every
  string with what it is used for. The C VM reads the image in place, so it costs flash
  (or wherever the host keeps the download), not RAM.
* **RAM** (`sizeof(fs_vm)`): computed for the defaults and for the smallest `FS_*`
  settings this program runs with. Stack and call depth come from a worst-case analysis of
  the bytecode; the report prints ready-to-use `-D` build flags, including the feature
  switches. For the GPS example that is 384 bytes instead of 6,080: it uses no floats,
  times or run-time strings, so those switches apply, a stored value is 5 bytes and
  there are no string slots. These settings are also load-time limits, so size them
  for the largest program you plan to download. Recursion has no static bound and keeps
  the defaults.
* **C VM code**: independent of the program, but set by the feature switches: about
  16 KB with everything, 8.2 KB with the smallest set (see the table above).

It also lists concrete savings: unused externs, functions, events, timers and globals,
globals that could be constants, the cost of names, and the debug section (`-g`).
The struct layout is checked against the C compiler and the minimum settings against
every test program (`tests/test_sizes.py`).

## License

MIT, see [LICENSE](https://github.com/RubenNorgaard/flowscript/blob/main/LICENSE).
