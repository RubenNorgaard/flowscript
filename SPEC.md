# FlowScript — Language & Bytecode Specification (v2)

FlowScript is a small, event-driven scripting language for describing the *internal
flow* of a larger system. The host system (firmware written in C, a Python service, …)
provides a stable set of functions and events; the *logic* is written in FlowScript,
compiled to compact bytecode and downloaded to the device.

Design goals, in priority order:

1. **Fast to learn** – a small set of keywords, four value types, no memory management.
2. **Fast to execute** – stack VM, 1-byte opcodes, no heap: strings live in a fixed
   pool inside the VM.
3. **Fast to visualize** – every statement is exactly one flowchart node, every
   condition is a tree of logic gates. Script ⇄ flowchart is a lossless round trip.
4. **Easy to embed** – the C VM is a single `.c/.h` pair, no `malloc`, no libc beyond
   `<stdint.h>`/`<string.h>`/`<math.h>`, fixed-size tables configurable by macros.

Non-goals: it is *not* a general-purpose language. There are no arrays, no heap, no
closures and no blocking calls.

v2 (this document) adds the types `float`, `str` and `time` to v1's single int type;
variables are declared with their type (`int x = 0`) instead of `var`. v1 images are
rejected by v2 VMs (format version 2).

---

## 1. A first example

```flowscript
flow tracker version 1

# Functions supplied by the C firmware
extern gps_speed() -> int
extern send_position(int mode)
extern led(int color)

event ignition(int active)
timer report

const LIMIT = 90
int overspeed = 0

initial state parked:
  on enter:
    led(0)
    start report every 10min

  on ignition(active):
    if active:
      goto_state driving

state driving:
  on enter:
    start report every 30s

  on report:
    if gps_speed() > LIMIT and not overspeed:
      overspeed = 1
      send_position(2)
    else:
      send_position(1)

  on ignition(active):
    if not active:
      goto_state parked
```

## 2. Lexical structure

* Source is UTF-8, line oriented. A statement ends at the end of the line.
* **Blocks are defined by indentation**, as in Python. A block header (`func`, `on`,
  `state`, `if`, `elif`, `else`, `while`) ends with `:` and is followed by one or more
  lines indented deeper than the header. The block ends at the first line that is
  indented less. Any indentation width works as long as it is consistent within a
  block; the formatter writes 2 spaces per level. **Tabs in indentation are an error**
  (they would make the meaning depend on the editor's tab width).
* An empty block is written `pass` (it produces no flowchart node and no code).
* Blank lines and comment-only lines never open or close a block.
* One statement per line: `if x: goto_state a` on a single line is not allowed, so every
  line remains exactly one flowchart node.
* Comments start with `#` and run to end of line. Comment lines directly above a
  statement (no blank line between) become that statement's **note**, shown as an
  annotation in the flowchart; the indentation of such comments does not matter.
  Other comments become stand-alone `comment` nodes in the innermost block that their
  own indentation allows. Comments directly above `elif` / `else` stay at the end of
  the preceding branch.
* Identifiers: `[A-Za-z_][A-Za-z0-9_]*`.
* Integers: `123`, `0x7F`, `0b1010`, `1_000_000`. Optional **time unit** suffix:
  `ms` (×1), `s` (×1000), `min` (×60000), `h` (×3600000). `30s` is the integer `30000`.
* Floats: `1.5`, `0.25`, `2.0e-3`, `1e6` (a digit before the point; no time units).
* `true` = 1, `false` = 0 (ints).
* String literals `"..."` (with `\n \t \" \\` escapes) are `str` values.
* `->` introduces a return type.

Keywords: `flow version extern event timer const int float str time func on state initial any_state
pass if elif else while goto_state raise_event start every stop return and or xor not true false`.
`int`, `float`, `str` and `time` also name the conversion functions (`float(n)`).

## 3. Values

Every value has one of four types, known at compile time:

| Type | Values | Literals |
|---|---|---|
| `int` | 32-bit signed integer, wrap-around arithmetic | `42`, `0x7F`, `30s`, `true` |
| `float` | IEEE 754 double | `2.5`, `1e-3` |
| `str` | immutable byte string (UTF-8 by convention), at most `FS_STR_LEN` bytes at run time | `"text"` |
| `time` | instant in UTC: milliseconds since 1970-01-01T00:00:00Z (int64), proleptic Gregorian calendar | `time(2026, 11, 1)`, `time("2026-11-01T12:00:00Z")` |

* Booleans are ints (0 = false, anything else = true; comparisons produce 0/1). A
  condition (`if`, `while`, `and`/`or`/`xor`/`not`) must be an int: write `x != 0.0`.
* int: division and modulo truncate toward zero (C semantics); division by zero is a fault.
  Shift counts are masked with `& 31`; `>>` is arithmetic.
* float: IEEE arithmetic, no faults (`1.0 / 0` is `inf`, `0.0 / 0` is `nan`); `%` is C `fmod`.
* **Promotion**: an int is converted to float where a float is expected (mixed arithmetic
  and comparisons, assignments, arguments, return values). Nothing else is implicit:
  `int(x)` truncates a float toward zero (fault 13 if out of range or nan).
* str: `+` joins, `== != < <= > >=` compare bytewise. Strings created at run time are
  held in `FS_STR_SLOTS` slots; see 7.1.
* time: `time + int`, `int + time` and `time - int` (milliseconds) give a time;
  `time - time` gives the difference in ms as an int, saturated to the int range (about
  ±24.8 days; use `float(t2) - float(t1)` seconds for longer spans). Times compare.
* Every type has a zero value — `0`, `0.0`, `""`, `time(1970, 1, 1)` — used for variables
  declared without a value, missing event arguments and stubbed externs.

### 3.1 Text

`str(x)` and the trace/debugger format are the same for both VMs:

* int: decimal (`-12`).
* float: 15 significant digits, trailing zeros removed; positional when the decimal exponent
  is in [-4, 15) (`0.1`, `2.5`, `100.0`, `0.0001`), else scientific (`1e+20`, `1.5e-07`);
  `nan`, `inf`, `-inf`, `-0.0`. The digits are computed with exact powers of ten (10^0..10^22)
  and one rounding (`floor(x · 10^k + 0.5)`), so both VMs give the same text.
* time: ISO 8601 UTC with milliseconds: `2026-11-01T12:00:00.000Z`.
* In traces a str is quoted: `"a \"b\"\n"` (escapes `\" \\ \n \t`, other bytes outside
  0x20–0x7E as `\xNN`).

`float(s)` parses `[+-]digits[.digits][(e|E)[+-]digits]`, `nan`, `inf` (the first 18
significant digits, scaled by exact powers of ten); `int(s)` parses `[+-]digits` within the
int range; `time(s)` parses `YYYY-MM-DD[(T| )HH:MM[:SS[.f]]][Z|±HH:MM]` (1 to 3 fraction
digits). Anything else faults with 13.

### 3.2 Built-in functions

| Function | Result |
|---|---|
| `int(float)`, `int(str)` | truncated / parsed int |
| `float(int)`, `float(str)`, `float(time)` | float (`float(time)` = seconds since 1970) |
| `str(int)`, `str(float)`, `str(time)` | text (3.1); `str(str)` is the string itself |
| `time(str)`, `time(y, m, d)`, `time(y, m, d, h, mi, s)` | a time (years 1–9999; fault 13 if invalid) |
| `now()` | the host clock (0 = 1970-01-01 without one) |
| `date(t)` | `t` at 00:00 UTC of its day |
| `year(t)` `month(t)` `day(t)` `hour(t)` `minute(t)` `second(t)` `millisecond(t)` `weekday(t)` | ints; weekday 0 = Monday |
| `len(s)` | bytes |
| `substr(s, start, count)` | bytes `start..start+count`, clamped to the string |
| `find(s, sub)` | first byte index of `sub`, or -1 |
| `abs(x)`, `min(a, b)`, `max(a, b)` | int or float (an int argument is promoted if the other is float) |
| `floor(x)` `ceil(x)` `round(x)` `sqrt(x)` `exp(x)` `log(x)` `pow(x, y)` | float; `round` is half away from zero |

A program's own `func` or `extern` with one of these names takes precedence. The
transcendental functions (`sqrt` excepted, which is exact) come from the platform's C
library, so their last bit may differ between platforms (never between the two VMs on
the same machine).

## 4. Program structure (top level)

| Declaration | Meaning |
|---|---|
| `flow NAME [version N]` | Optional header. Name and version are stored in the bytecode so the host can check them. Must be first. |
| `extern NAME(int a, str b) [-> TYPE]` | Host function with typed parameters; `-> TYPE` if it returns a value (without it, calling it in an expression is a compile error). Bound by name when the bytecode is loaded. |
| `event NAME[(int a, float b)]` | An event the host (or the script, via `raise_event`) can raise. Up to 8 typed parameters. |
| `timer NAME` | A VM-managed timer. A timer is also an event with the same name. |
| `const NAME = expr` | Compile-time constant of any type (the expression must be constant; pure built-ins fold, e.g. `time(2026, 1, 1)`). |
| `TYPE NAME [= expr]` | Global variable (`int`, `float`, `str` or `time`); the initial value must be constant (default: the zero value). Only event handlers can use it, not functions. |
| `func NAME(int a, float b) [-> TYPE]:` + block | User function (sub-process). It cannot read or write globals: data comes in as parameters and goes out with `return` (or `raise_event`), so only event handlers change program state. Falling off the end returns the zero value of `TYPE`. |
| `any_state:` + block | Handlers that apply in every state (`on boot` lives here). At most one. |
| `initial state NAME:` + block | The state the program starts in (required when there are 2+ states). |
| `state NAME:` + block | A state: its variables (`TYPE NAME = value`, first), then its `on` handlers (or just `pass`). |

Every event handler is inside a `state` or inside `any_state`; there are no loose
top-level handlers. `any_state` is the implicit outer state that contains all states and
is never left, so the program is always a two-level hierarchical state machine.

### 4.1 States and dispatch

* The **initial state** is the one marked `initial state NAME:`. With two or more
  states exactly one must be marked, so reordering states never changes where the
  program starts; a lone state needs no marker. The compiler gives the initial state
  index 0 in the state table (the VM always starts in state 0); the other states keep
  source order. `on boot` may still `goto_state` elsewhere, in which case the initial
  state is never entered. A program without states is a
  plain event-handler program whose handlers all live in `any_state`.
* When an event arrives, the handler in the **current state** runs if it exists;
  otherwise the `any_state` handler for that event runs. Events without a handler are
  dropped. The compiler **warns** when a state handler replaces an `any_state` handler
  for the same event (the program still compiles: overriding is allowed, the warning
  just makes it visible). The debugger's States view shows a state × event table with
  the handler that runs for every combination.
* Built-in events:
  * `boot` – `any_state` only; runs once when the VM is started, before the initial
    state is entered.
  * `enter` / `exit` – states only (`any_state` is never entered or left); run when
    the state is entered / left.
* Handlers **run to completion**. There is no blocking `wait`; use timers.

### 4.2 State variables

```flowscript
state driving:
  int overspeed_count = 0     # state variable: before the handlers

  on report:
    overspeed_count = overspeed_count + 1
```

* A variable declared at the top of a state block belongs to that state. Only that state's
  handlers can read or write it; functions and `any_state` handlers cannot (pass it as
  an argument if a function needs it). Prefer state variables over globals for data
  that only matters while the program is in one state.
* It is **reset to its initial value every time the state is entered** (including
  self-transitions and the initial entry after boot), before the state's own
  `on enter` body runs. Leaving a state therefore discards its variables, just as it
  stops its timers.
* The name must not clash with any top-level declaration, and locals and handler
  parameters may not hide it. Two different states may use the same name.
* `any_state` has no variables: data shared by every state is an ordinary global.
* Implementation: each state variable is a global slot (debug name `STATE.NAME`), so
  the VM and bytecode are unchanged. The compiler emits the reset (`PUSH value;
  STOREG slot` per variable) at the start of the state's `on enter` handler, and
  generates an `on enter` handler containing only the reset if the state has none.

### 4.3 Handler parameters

`on motion(level)` names the event's parameters (their types come from the `event`
declaration). If the parameter list is omitted the names from the `event` declaration
are used. A handler may not declare more parameters than the event has.

## 5. Statements (= flowchart nodes)

| Statement | Flowchart symbol |
|---|---|
| `x = expr` | **Process** rectangle |
| `TYPE x [= expr]` | **Process** rectangle (declares a local) |
| `name(args)` (extern) | **I/O** parallelogram |
| `name(args)` (func) | **Predefined process** (double-sided rectangle) |
| `if c:` … `elif c:` … `else:` … | **Decision** diamond (condition drawn as logic gates) |
| `while c:` … | **Loop** diamond with back edge |
| `goto_state STATE` | **Transition** connector (circle) – ends the handler |
| `raise_event EVENT[(args)]` | **Signal** (flag / pentagon) |
| `start TIMER ms` / `start TIMER every ms` | **Timer** (hourglass) – one-shot / periodic |
| `stop TIMER` | **Timer** (hourglass, crossed) |
| `return [expr]` | **Terminator** |
| `# comment` (stand-alone) | **Annotation** |
| `on EVENT` (handler head) | **Event** (rounded start terminal) |
| `state NAME` | **State** container / swim-lane |

Notes:

* `goto_state` ends the current handler immediately (also from inside a function). The VM
  then runs the old state's `exit` handler, switches state, and runs the new state's
  `enter` handler. A `goto_state` inside an `exit` handler is ignored. Transitions are
  processed before any queued event.
* `raise_event` queues the event for the script **and** notifies the host through the
  `on_emit` callback (so the script can signal the system, e.g. `raise_event alarm(3)`).
* `start` restarts a timer if it is already running. A timer belongs to the state whose
  handler started it (also when started from a function that handler calls) and is
  stopped automatically when that state is left (see 8.3). Timers started by global
  handlers in `any_state` keep running across states.
* Locals (`float x = …`) are function-scoped; a name can only be declared once
  per function/handler, and is visible from its declaration on. Every local starts at
  its type's zero value, so one declared in a branch that did not run still holds a
  value of its type.
* `start T ms`: `ms` is an int.
* `while` loops are allowed; every handler run has an **instruction budget**
  (default 10 000) that faults runaway loops.

## 6. Expressions (= logic gates)

Precedence, lowest first:

| Level | Operators | Visual |
|---|---|---|
| 1 | `or` | OR gate |
| 2 | `xor` | XOR gate |
| 3 | `and` | AND gate |
| 4 | `not` (prefix) | NOT gate |
| 5 | `== != < <= > >=` (non-associative) | compare leaf |
| 6 | `\|` | |
| 7 | `^` | |
| 8 | `&` | |
| 9 | `<< >>` | |
| 10 | `+ -` (`+` also joins strings) | |
| 11 | `* / %` | |
| 12 | unary `-` `~` | |
| 13 | literals, names, calls, `( … )` | |

`and` / `or` short-circuit. `not`, `and`, `or`, `xor` always produce 0 or 1.

In the flowchart, the condition of a decision/loop is split at `and/or/xor/not` into a
gate tree; everything below that level (e.g. `gps_speed() > LIMIT`) is a leaf box.

**Magic numbers.** The compiler warns when a number literal other than `-1`, `0` or `1`
is compared directly (`level > 3`), suggesting a named constant
(`const MOTION_ALWAYS = 8` … `level >= MOTION_ALWAYS`). Constants cost nothing at run
time: they are folded into the bytecode. Literals in arithmetic (`LIMIT / 2`), in
declarations, in timer durations and as call arguments are not flagged.

## 7. Execution model

```
host                       VM
----                       --
fs_load(bytecode)  ───────▶ verify header, tables
fs_bind("led", fn) ───────▶ resolve externs by name (all must be bound)
fs_start()         ───────▶ queue: boot, enter(initial state)
fs_post(ev, args)  ───────▶ event queue (fixed size, typed arguments)
fs_tick(ms)        ───────▶ timers → event queue
fs_run()           ───────▶ dispatch & execute until idle
fs_step()          ───────▶ one dispatch or one instruction (debugger)
     ◀── extern calls, emit notifications, fault reports
```

**Step semantics** (identical in every VM, used by the debugger and the conformance
tests): if no handler is active, one `step` performs one *dispatch*. Otherwise it
executes exactly one instruction.

**Chain limit.** The VM counts dispatches since it was last idle (the counter is reset
whenever a dispatch finds nothing to do). If a dispatch is due while the counter is
already `MAX_CHAIN` (256, `FS_MAX_CHAIN` in C), the VM instead reports fault 9 with
pc -1, clears `boot_pending`, `pending`, `transition` and the event queue, resets the
counter and is idle. This stops scripts that never go idle, such as two `enter`
handlers that `goto_state` each other or a handler that raises its own event, which the
per-handler budget cannot catch. The current state is kept; timers keep running.
Every run to idle is therefore bounded: at most `MAX_CHAIN` handler runs of at most
`BUDGET` instructions each.

`start()` sets `boot_pending` and, if the program has states, `pending = 0` (the
initial state). A *dispatch* does the first applicable of:

1. `boot_pending`: clear it; if a `boot` handler exists (in `any_state`), start it and stop here.
2. `pending` set: `transition = pending`, clear `pending`. If there is a current
   state with an `exit` handler, start it (`goto_state` inside it is ignored) and stop here.
3. `transition` set: `state = transition`, clear `transition`, start the state's
   `enter` handler if it has one. Stop here (counts as a dispatch even without handler).
4. Pop queued events until one has a handler (state handler, else global); start it.
5. Otherwise the VM is idle.

Because `boot` runs before the initial transition, a `goto_state` in `boot` chooses the
initial state. Starting a handler means: stack = event args (missing args are 0) +
zeros up to `nlocals`, one frame `{return_pc: none, base: 0}`, pc = handler offset,
budget counter = 0. The budget is checked before each instruction (fault 4 when the
counter would exceed the budget). A fault aborts the handler; the VM then continues
with the next dispatch (so a faulting `boot` still enters the initial state).

Clarifications:

* A `raise_event` into a full queue reports fault 7 but does **not** abort the handler (the
  event is dropped).
* Calling an unbound extern faults with code 8 at the call. Hosts should check
  `fs_unbound()` after binding and refuse to start a program that needs functions the
  firmware does not provide.
* `FS_MAX_FRAMES` (16) includes the handler's own frame, so functions can nest 15 deep.
* Compile-time limits: 16 parameters per function/extern, 8 per event, 32 parameters +
  locals per handler/function (the VM stack is 64 slots), 64 KiB code.

**Faults** (stack overflow, division by zero, budget exceeded, …) abort the current
handler, are reported to the host, and the VM continues with the next event. A faulty
script can therefore never take down the host.

The reported fault *pc* is the address of the faulting instruction, or -1 for a
queue-full fault raised outside a handler (host `post`/`tick`) and for fault 9.

| Fault | Code |
|---|---|
| stack overflow | 1 |
| stack underflow | 2 |
| division by zero | 3 |
| instruction budget exceeded | 4 |
| bad opcode / operand | 5 |
| call depth exceeded | 6 |
| event queue full | 7 |
| unbound extern | 8 |
| dispatch chain limit (script never idle) | 9 |
| type mismatch (bad bytecode, or an extern returned / host posted the wrong type) | 10 |
| out of string slots | 11 |
| string longer than `FS_STR_LEN` | 12 |
| invalid value (`int("x")`, `int(nan)`, `time(2026, 2, 30)`, …) | 13 |

### 7.1 Values in the VM and strings

Every stack slot, global and event argument holds a typed value (a type tag plus 8
bytes). The VM checks the tags it depends on: conditions and timer durations must be
ints, an extern's arguments must have its declared types, an extern's result must have
its return type (an int is accepted for a float; for a str the host may return a C
string, which is copied), arithmetic operands must match. A mismatch is fault 10.
Compiled code never mismatches; the checks keep a corrupt image or a wrong host function
from being misread.

Strings created at run time (`+`, `str()`, `substr()`, extern results, host event
arguments) take one of `FS_STR_SLOTS` slots of `FS_STR_LEN` bytes. String constants live in
the image and take no slot. When no slot is free the VM **collects**: it frees every slot
not referenced from a global, the value stack or the event queue (mark and sweep), then
tries again; if all are still in use the instruction faults with 11. A string longer than
`FS_STR_LEN` faults with 12 before any slot is taken. While an instruction or extern runs,
its operands stay on the stack, so they are never collected under it. Both VMs take the
lowest free slot and collect at the same moments, so faults 11 and 12 happen at the same
instruction.

`fs_post` copies the host's string arguments into slots (the queue entry is reserved
first, so the strings already copied are roots); if there is no room the event is not
queued and fault 11 or 12 is reported like a queue-full fault 7. A posted argument of the
wrong type is refused (`FS_ERR_ARG`) without a fault.

`now()` reads the host clock (`fs_set_clock`, `VM(clock=...)`); without one it is 0.

## 8. Bytecode file format (`.fsb`)

All integers little-endian. Strings are stored as `u16 len, bytes, 0x00` so a C host
can point straight into the buffer.

```
Header (32 bytes)
  0  u8[4]  magic "FSB1"
  4  u16    format version (2)
  6  u16    flags            bit0: debug section present
                              bit1: needs float values     (refused by FS_NO_FLOAT builds)
                              bit2: needs time values      (refused by FS_NO_TIME builds)
                              bit3: makes strings at run time: CONCAT, a built-in returning
                                    str, an extern returning str or an event with a str
                                    parameter (refused by FS_NO_STR builds)
  8  u16    program name     (string index, 0xFFFF = none)
 10  u16    program version
 12  u16    n_strings
 14  u16    n_globals
 16  u16    n_externs
 18  u16    n_events
 20  u16    n_states
 22  u16    n_timers
 24  u16    n_funcs
 26  u16    n_handlers
 28  u32    code_size

Sections, in this order, immediately after the header:
  strings   n_strings × { u16 len; u8 bytes[len]; u8 0 }
  globals   n_globals × { u8 type; 8 bytes }   initial value (see below)
  externs   n_externs × { u16 name; u8 argc; u8 return type; u32 parameter types }
  events    n_events  × { u16 name; u8 argc; u8 reserved; u16 parameter types }
  states    n_states  × { u16 name }
  timers    n_timers  × { u16 event }
  funcs     n_funcs   × { u16 name; u8 argc; u8 nlocals; u16 code_offset }
  handlers  n_handlers× { u16 state (0xFFFF = global); u16 event; u8 argc; u8 nlocals; u16 code_offset }
  code      code_size bytes
  debug     (if flags bit0)
            u16 n_line_entries × { u16 pc; u16 line; u16 node }
            n_globals × u16 name (string index)
```

Events 0, 1, 2 are always the built-ins `boot`, `enter`, `exit` (argc 0). Each timer
owns one event (named like the timer). The code size is limited to 64 KiB (jump targets
are absolute `u16`).

Type codes: 0 int, 1 float, 2 str, 3 time; an extern's return type 4 = returns nothing.
Parameter types are packed 2 bits each, the first parameter in the lowest bits (externs
up to 16 parameters, events up to 8). A global's 8 bytes are an i64 for int (the low
32 bits are used) and time, an IEEE double for float, and the string-table index for
str. Strings may hold any bytes (constant folding can produce non-UTF-8).

### 8.1 Instruction set

Every instruction is a 1-byte opcode followed by its operands.

| Op | Hex | Operand | Stack effect | Notes |
|---|---|---|---|---|
| NOP | 00 | – | – | |
| PUSH | 01 | i32 | → v | |
| PUSH8 | 02 | i8 | → v | small constant |
| PUSHS | 03 | u16 | → s | str constant (string table index) |
| POP | 04 | – | v → | |
| DUP | 05 | – | v → v v | |
| PUSHF | 06 | f64 | → f | float constant |
| PUSHT | 07 | i64 | → t | time constant (ms) |
| LOADG | 10 | u16 | → v | global |
| STOREG | 11 | u16 | v → | |
| LOADL | 12 | u8 | → v | local (frame relative) |
| STOREL | 13 | u8 | v → | |
| ADD SUB MUL DIV MOD | 20–24 | – | a b → r | int/int, float/float; ADD/SUB also time±int (ADD int+time), SUB time−time → int ms |
| NEG | 25 | – | a → r | int or float |
| BAND BOR BXOR | 26–28 | – | a b → r | ints |
| BNOT | 29 | – | a → r | int |
| SHL SHR | 2A 2B | – | a b → r | ints |
| CONCAT | 2C | – | a b → s | two strs → a new string (slot) |
| EQ NE LT LE GT GE | 30–35 | – | a b → 0/1 | two values of one type (str: bytewise) |
| NOT | 36 | – | a → 0/1 | int |
| BOOL | 37 | – | a → 0/1 | int, `a != 0` |
| JMP | 40 | u16 | – | absolute |
| JZ | 41 | u16 | v → | jump if v == 0 (int) |
| JNZ | 42 | u16 | v → | jump if v != 0 (int) |
| CALL | 50 | u16 | args → result | user function |
| RET | 51 | – | v → | return v (ends handler at base frame) |
| RET0 | 52 | – | – | return 0 |
| XCALL | 53 | u16 | args → result | extern (int 0 if it returns nothing) |
| BUILTIN | 54 | u8 | args → result | built-in function by id (`values.BUILTINS`; overloads have their own id) |
| GOTO | 60 | u16 | – | request transition, end handler |
| EMIT | 61 | u16 | args → | queue event + notify host |
| TSTART | 62 | u16 | ms → | one-shot timer (int ms) |
| TEVERY | 63 | u16 | ms → | periodic timer (int ms) |

Operand types other than those listed fault with 10. An int is promoted to float by
`BUILTIN 0` (`float(int)`); the compiler emits a small integral float constant as
`PUSH8`/`PUSH` + `BUILTIN 0` (4–7 bytes instead of 9).
| TSTOP | 64 | u16 | – | |

### 8.2 Frames

A frame is `{return_pc, base}`. On `CALL f` the caller has pushed `argc` arguments;
the VM pushes `nlocals - argc` int zeros (the compiler initialises locals of other types), and locals are `stack[base + i]`. `RET` pops the
result, truncates the stack to `base`, pops the frame and pushes the result. A handler
is started like a call with the event arguments as its first locals.

### 8.3 Timers

Each timer has `active`, `periodic`, `period`, `remaining` and `owner`. `start`/`start
every` set them and `owner` = the state of the running handler (none for a global
handler). When a dispatch leaves a state (step 3 of the dispatch rules, just before
`state = transition`, so after its `exit` handler and also for a transition back to
the same state), every active timer owned by that state is stopped.

There are two ways to run the timers; the host picks one before `start()`:

* **Built-in** (default): `tick(ms)` visits timers in index order. For each running
  timer: `remaining -= ms`; if `remaining <= 0` the timer event is queued once, then a
  periodic timer does `remaining += period` (if still `<= 0`, `remaining = period`), a
  one-shot timer stops. The host must call `tick` regularly.
* **Host-managed** (`fs_set_timer_handler` / `on_timer`): the VM never counts time and
  `tick` does nothing. It calls the host's handler with *start* (timer, ms, periodic)
  for every `start`, and with *stop* whenever an active timer stops (`stop`, leaving
  the owning state, `reset`). The host runs the timer however it likes (RTC alarm,
  low-power timer, RTOS timer) so the CPU only wakes up when something is due, and
  calls `timer_expired(timer)` when it fires: if the timer is no longer active the
  call is ignored (a late expiry after a stop); otherwise a one-shot timer becomes
  inactive and the timer event is queued (fault 7 if the queue is full). Periodic
  timers are re-armed by the host. What happens for `ms <= 0` is up to the host
  (firing as soon as possible is recommended).

## 9. Visual model

The visual model is the **AST serialised as JSON** (see `flowscript/ast.py`). Every
node has a stable `id`, a `type` and type-specific fields; statements carry an optional
`note`. The flowchart is a pure function of this JSON (auto-layout), and the formatter
turns the JSON back into canonical source. Editing the chart edits the JSON; editing
the source re-parses into JSON. The compiler emits a *pc → node id* table, which is how
the debugger knows which flowchart node and which logic gate is executing.
