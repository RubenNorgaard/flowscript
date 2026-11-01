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
 bytecode (.fsb) ──▶  C VM (firmware)   flowscript.c, no malloc, ~6 KB Thumb-2
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

Requirements: Python 3.8+ (stdlib only), any C99 compiler. No other dependencies.

## The language in one page

```flowscript
flow tracker version 3          # name + version are stored in the bytecode

extern gps_speed()              # functions the host provides (bound by name at load)
extern send_position(reason)
event ignition(active)          # events the host raises (up to 8 int args)
timer report                    # VM-managed timer; also an event called "report"
const LIMIT = 90                # compile-time constant
var reports = 0                 # global variable (int32), handlers only

func notify(reason, count):     # sub-process: sees params, consts, externs, not vars
  send_position(reason)
  return count + 1              # the result goes back to the handler

any_state:                      # handlers that apply in every state
  on boot:                      # runs once at start
    start report every 30s      # time units: ms, s, min, h

initial state parked:           # the program starts here (required with 2+ states)
  on ignition(active):
    if active:
      goto_state driving        # ends the handler; runs exit/enter handlers

state driving:
  var slow_reports = 0          # state variable: only visible here, reset on each entry

  on report:                    # replaces an any_state 'on report' here
    var speed = gps_speed()     # local variable
    if speed > LIMIT and not reports > 100:
      reports = notify(2, reports)
    elif speed == 0:
      raise_event ignition(0)   # queued for the script + host notified
    else:
      slow_reports = slow_reports + 1
      reports = notify(1, reports)
```

Blocks are defined by indentation, as in Python: a header line ends with `:` and its
body is indented below it (the formatter uses 2 spaces; tabs are rejected). `pass`
marks an empty block. One statement per line, so every line is one flowchart node.

One type (int32), `if/elif/else`, `while`, `goto_state`, `raise_event`, `start/stop`, `return`,
functions, and the operators `or xor and not == != < <= > >= | ^ & << >> + - * / % ~`.
That is the whole language. See [SPEC.md](SPEC.md) for the exact semantics and the
bytecode format.

| Statement | Flowchart symbol |
|---|---|
| `on EVENT` | event (hexagon) |
| `state S` / `any_state` | state container / dashed container around all states |
| `x = …`, `var x = …` | process (rectangle) |
| state `var` | "state variables" card inside the state container |
| `extern_fn(…)` | I/O (parallelogram) |
| `func_name(…)` | predefined process (double-sided rectangle) |
| `if` / `elif` / `else` | decision (diamond) fed by a logic-gate tree |
| `while` | decision with a back edge |
| `and` `or` `xor` `not` | AND / OR / XOR / NOT gates |
| `goto_state S` | state transition (red terminator) |
| `raise_event E` | signal (flag) |
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

static int32_t x_gps_speed(fs_vm *vm, const int32_t *a, int n, void *u) { return gps_speed_kmh(); }

void logic_init(const uint8_t *image, size_t len) {
    if (fs_load(&vm, image, len) != FS_OK) { /* reject download, keep old logic */ }
    fs_bind(&vm, "gps_speed", x_gps_speed);
    /* ... bind every extern; fs_unbound(&vm) names any you forgot ... */
    fs_start(&vm);
    fs_run(&vm, 100000);
}

void on_ignition(int on) {                         /* firmware event -> script */
    int32_t arg = on;
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

Like every VM call, `fs_timer_expired` and `fs_run` must not interrupt a running
`fs_run`, so call them from one task or the main loop, not directly from an ISR.
A timer belongs to the state whose handler started it and stops automatically when
that state is left; in this mode the VM then calls `on_timer(..., start = 0, ...)`.

See [`c/flowscript.h`](c/flowscript.h) for the full API and `c/fsrun.c` for a complete
host. The loader validates the whole image and every run-time operand, faults abort only
the current handler, each handler has an instruction budget, and a script that keeps
itself busy forever (e.g. two states whose `enter` handlers `goto_state` each other) is stopped
after 256 handler runs in a row. A bad download can therefore never take down the
firmware.

## Repository layout

```
SPEC.md                 language + bytecode specification (normative)
flowscript/             Python toolchain
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
cd c && make test                         # C API test + C/Python conformance (normal + ASan/UBSan)
cd c && make fuzz                         # corrupt-image fuzzing against the ASan build
cd c && make size                         # code size of the VM
```

C VM footprint (default sizes): **about 6 KB** of code for Cortex-M4 (`-Os`, Thumb-2; 5,782 bytes of `.text` measured with clang),
no data/bss of its own, and a `fs_vm` struct of 2,648 bytes RAM on a 32-bit target with the
default `FS_*` macros in `flowscript.h`. Most of that is headroom: see below.

## Optimizing size

`python3 -m flowscript size FILE.fs` (or `compile --size`, or the **Size** tab in the
debugger's bytecode pane) shows the three budgets separately:

* **Image** (the `.fsb`): bytes per section, code per handler/function, and every
  string with what it is used for. The C VM reads the image in place, so it costs flash
  (or wherever the host keeps the download), not RAM.
* **RAM** (`sizeof(fs_vm)`): computed for the defaults and for the smallest `FS_*`
  settings this program runs with. Stack and call depth come from a worst-case analysis of
  the bytecode; the report prints ready-to-use `-D` build flags. For the GPS example that
  is 376 bytes instead of 2,648. These settings are also load-time limits, so size them
  for the largest program you plan to download. Recursion has no static bound and keeps
  the defaults.
* **C VM code**: fixed (about 6 KB), independent of the program.

It also lists concrete savings: unused externs, functions, events, timers and globals,
globals that could be constants, the cost of names, and the debug section (`-g`).
The struct layout is checked against the C compiler and the minimum settings against
every test program (`tests/test_sizes.py`).
