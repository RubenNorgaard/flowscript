/*
 * FlowScript virtual machine (C99, no malloc, no stdio).
 *
 * Behaviour is identical to the reference VM in flowscript/vm.py (see SPEC.md
 * sections 7 and 8).  The library depends on <stdint.h>, <stddef.h>,
 * <string.h> and <math.h> (floor, ceil, sqrt, exp, log, pow, fmod: link -lm).
 * Build with -ffp-contract=off so float results match the Python VM bit for bit.
 *
 * Values are typed (fs_value): int (int32), float (double), str and time
 * (int64 ms since 1970-01-01T00:00:00Z).  Run-time strings live in a fixed pool
 * of FS_STR_SLOTS slots of FS_STR_LEN bytes inside fs_vm; unused ones are
 * reclaimed (mark and sweep over globals, stack and queue) when the pool is full.
 *
 * ---------------------------------------------------------------------------
 * Typical use
 * ---------------------------------------------------------------------------
 *
 *   static fs_vm vm;                         // ~9 KB RAM with default sizes
 *
 *   if (fs_load(&vm, image, image_len) != FS_OK) { ... reject download ... }
 *   fs_bind(&vm, "led", my_led);             // bind every extern by name
 *   if (fs_unbound(&vm)) { ... missing host function ... }
 *   fs_set_callbacks(&vm, on_emit, on_fault, user);
 *   fs_start(&vm);
 *   for (;;) {
 *       fs_value on = fs_int(1);
 *       fs_post(&vm, fs_event_id(&vm, "ignition"), &on, 1);
 *       fs_tick(&vm, elapsed_ms);
 *       fs_run(&vm, 100000);                 // dispatch & execute until idle
 *   }
 *
 * The image buffer passed to fs_load() is NOT copied: it must stay valid (and
 * unchanged) for as long as the VM is used.  It may be unaligned and live in
 * flash; all multi-byte values are read byte by byte (little endian).
 *
 * fs_load() fully validates the image: a corrupt download is rejected with a
 * negative error code.  Run-time operand errors (bad jump target, bad indices,
 * stack over/underflow, ...) are reported as faults (SPEC 7) and abort only the
 * current handler; they never crash the host.
 *
 * Timers: by default the VM keeps time itself and the host calls fs_tick().
 * For low-power firmware, register fs_set_timer_handler() instead: the VM then
 * asks the host to start/stop each timer (RTC alarm, low-power timer, RTOS
 * timer, ...) and the host calls fs_timer_expired() when one fires, so the CPU
 * only wakes up when something is due.
 *
 * Callbacks must not call fs_step()/fs_run()/fs_load()/fs_start() re-entrantly.
 * They may call fs_post()/fs_tick()/fs_timer_expired() and all read-only accessors.
 */
#ifndef FLOWSCRIPT_H
#define FLOWSCRIPT_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- compile-time sizes (defaults match flowscript/vm.py) ---------------- */
#ifndef FS_STACK_SIZE
#define FS_STACK_SIZE 64        /* value stack slots                        */
#endif
#ifndef FS_MAX_FRAMES
#define FS_MAX_FRAMES 16        /* call frames incl. the handler's frame    */
#endif
#ifndef FS_QUEUE_SIZE
#define FS_QUEUE_SIZE 16        /* pending events                           */
#endif
#ifndef FS_BUDGET
#define FS_BUDGET 10000         /* instructions per handler run             */
#endif
#ifndef FS_MAX_CHAIN
#define FS_MAX_CHAIN 256        /* dispatches in a row without becoming idle */
#endif
#ifndef FS_MAX_EXTERNS
#define FS_MAX_EXTERNS 64       /* fs_load rejects images with more         */
#endif
#ifndef FS_MAX_GLOBALS
#define FS_MAX_GLOBALS 256
#endif
#ifndef FS_MAX_TIMERS
#define FS_MAX_TIMERS 16
#endif
#ifndef FS_MAX_EVENT_ARGS
#define FS_MAX_EVENT_ARGS 8
#endif
#ifndef FS_STR_SLOTS
#define FS_STR_SLOTS 16         /* run-time strings alive at once           */
#endif
#ifndef FS_STR_LEN
#define FS_STR_LEN 64           /* bytes per run-time string (+ a NUL)      */
#endif

/* ---- optional features ---------------------------------------------------
 * Define any of these to leave a feature out of the VM: less code, and smaller
 * values (int + str only: 8 bytes each instead of 16).  An image that needs a
 * left-out feature is refused by fs_load() with FS_ERR_FEATURE; the compiler
 * records what a program needs, and `flowscript size` lists the switches it allows.
 *   FS_NO_FLOAT  float values (also drops the libm dependency)
 *   FS_NO_TIME   time values and now()
 *   FS_NO_STR    strings made at run time: +, str(), substr(), str results and
 *                arguments from the host.  String literals ("text") still work.
 *   FS_NO_TEXT   text conversion of floats and times in scripts: str(float),
 *                str(time), float("..."), time("...").  The types stay, and so do
 *                int <-> text.  fs_format() and fs_parse_float()/fs_parse_time()
 *                remain for the host; a device that never calls them should link
 *                with -ffunction-sections -Wl,--gc-sections so they are dropped.
 */
#if defined(FS_NO_FLOAT)
#define FS_HAS_FLOAT 0
#else
#define FS_HAS_FLOAT 1
#endif
#if defined(FS_NO_TIME)
#define FS_HAS_TIME 0
#else
#define FS_HAS_TIME 1
#endif
#if defined(FS_NO_STR)
#define FS_HAS_STR 0
#else
#define FS_HAS_STR 1
#endif
#if defined(FS_NO_TEXT)
#define FS_HAS_TEXT 0
#else
#define FS_HAS_TEXT 1
#endif

/* ---- return / error codes ------------------------------------------------ */
#define FS_OK             0
#define FS_ERR_MAGIC     (-1)   /* not an FSB1 image                          */
#define FS_ERR_VERSION   (-2)   /* unsupported format version                 */
#define FS_ERR_TRUNCATED (-3)   /* a section runs past the end of the image   */
#define FS_ERR_LIMIT     (-4)   /* a table exceeds a FS_MAX_* macro           */
#define FS_ERR_INDEX     (-5)   /* bad string / event / state index in a table */
#define FS_ERR_OFFSET    (-6)   /* func/handler code offset >= code_size      */
#define FS_ERR_ARG       (-7)   /* bad argument (NULL, unknown event, ...)    */
#define FS_ERR_FULL      (-8)   /* event queue full (fault 7 was reported)    */
#define FS_TIMER_STOPPED   1    /* fs_timer_expired: timer not running, ignored */
#define FS_ERR_NOTFOUND  (-9)   /* fs_bind: no extern with that name          */
#define FS_ERR_NOTLOADED (-10)  /* no valid image loaded                       */
#define FS_ERR_MEM       (-11)  /* fs_post: no room for a string argument (fault 11/12 reported) */
#define FS_ERR_FEATURE   (-12)  /* the image needs a feature this build left out (FS_NO_*) */

/* ---- fs_step() results ----------------------------------------------------- */
#define FS_IDLE       0         /* nothing to do                               */
#define FS_DISPATCHED 1         /* a dispatch happened, no instruction executed */
#define FS_RUNNING    2         /* one instruction executed, handler active     */
#define FS_DONE       3         /* one instruction executed, handler finished   */
#define FS_FAULT      4         /* the instruction faulted, handler aborted     */

/* ---- fault codes (SPEC 7) ------------------------------------------------- */
#define FS_FAULT_STACK_OVERFLOW  1
#define FS_FAULT_STACK_UNDERFLOW 2
#define FS_FAULT_DIV_ZERO        3
#define FS_FAULT_BUDGET          4
#define FS_FAULT_BAD_OPCODE      5  /* bad opcode or operand                    */
#define FS_FAULT_CALL_DEPTH      6
#define FS_FAULT_QUEUE_FULL      7
#define FS_FAULT_UNBOUND         8
#define FS_FAULT_CHAIN           9  /* script never idle (goto_state/emit loop): its
                                       pending transition and queue are dropped */
#define FS_FAULT_TYPE           10  /* a value of the wrong type (bad bytecode, or an
                                       extern returned the wrong type)          */
#define FS_FAULT_NO_STRINGS     11  /* every string slot is in use              */
#define FS_FAULT_STR_LONG       12  /* a string longer than FS_STR_LEN          */
#define FS_FAULT_VALUE          13  /* invalid conversion: int("x"), time(2026, 13, 1), ... */

/* ---- values ------------------------------------------------------------------ */
#define FS_INT   0
#define FS_FLOAT 1
#define FS_STR   2
#define FS_TIME  3
#define FS_CSTR  4              /* host -> VM only: a NUL-terminated C string to copy
                                   (extern results, fs_post arguments); see fs_cstr() */
#define FS_NONE_TYPE 4          /* fs_extern_ret(): the extern returns nothing */

typedef struct {
    uint8_t type;               /* FS_INT, FS_FLOAT, FS_STR, FS_TIME (FS_CSTR from the host) */
    union {
        int32_t i;              /* FS_INT                                         */
#if FS_HAS_FLOAT
        double f;               /* FS_FLOAT                                       */
#endif
#if FS_HAS_TIME
        int64_t t;              /* FS_TIME: ms since 1970-01-01T00:00:00Z (UTC)    */
#endif
        int32_t s;              /* FS_STR: >= 0 image string, < 0 run-time slot   */
#if FS_HAS_STR
        const char *c;          /* FS_CSTR                                        */
#endif
    } as;
} fs_value;

static inline fs_value fs_int(int32_t i) { fs_value v; memset(&v, 0, sizeof(v)); v.type = FS_INT; v.as.i = i; return v; }
#if FS_HAS_FLOAT
static inline fs_value fs_float(double f) { fs_value v; v.type = FS_FLOAT; v.as.f = f; return v; }
#endif
#if FS_HAS_TIME
static inline fs_value fs_time(int64_t ms) { fs_value v; v.type = FS_TIME; v.as.t = ms; return v; }
#endif
#if FS_HAS_STR
/* A C string handed to the VM, which copies it into a string slot (the pointer
 * only has to be valid until the extern returns / fs_post returns). */
static inline fs_value fs_cstr(const char *s) { fs_value v; v.type = FS_CSTR; v.as.c = s ? s : ""; return v; }
#endif

/* How the VM stores a value inside fs_vm: the payload only.  The type tag lives
 * in a parallel byte array (gtypes, stypes, fs_qentry.types), so a stored value
 * costs sizeof(fs_cell) + 1 bytes instead of sizeof(fs_value).  Hosts never
 * see cells: every API call takes and returns fs_value. */
typedef union {
    int32_t i;
    int32_t s;
#if FS_HAS_FLOAT
    double f;
#endif
#if FS_HAS_TIME
    int64_t t;
#endif
} fs_cell;

#define FS_NONE16 0xFFFFu

typedef struct fs_vm fs_vm;

/* Host function.  args points to argc values of the declared types (read
 * strings with fs_str()); the pointer and the strings are only valid during the
 * call.  Return a value of the declared return type (an int is accepted for a
 * float; return fs_cstr(text) or one of the arguments for a str); the result of
 * an extern declared without a return type is ignored.  A wrong type faults
 * with FS_FAULT_TYPE.  fs_current_extern(vm) tells which extern is being
 * called (useful for generic stubs). */
typedef fs_value (*fs_extern_fn)(fs_vm *vm, const fs_value *args, int argc, void *user);
/* `raise_event EVENT(args)` notification; called before the event is queued. */
typedef void (*fs_emit_fn)(fs_vm *vm, int event, const fs_value *args, int argc, void *user);
/* now(): ms since 1970-01-01T00:00:00Z (UTC).  Without a clock now() is 0. */
typedef int64_t (*fs_clock_fn)(fs_vm *vm, void *user);
/* Fault report; pc = address of the faulting instruction, or -1 for a
 * queue-full fault raised by fs_post()/fs_tick() outside a handler. */
typedef void (*fs_fault_fn)(fs_vm *vm, int code, int pc, void *user);
/* Host-managed timers (see fs_set_timer_handler).
 *   start = 1: (re)start timer `timer`: fire after `ms` milliseconds, and then
 *              every `ms` again if `periodic`.  Replaces a running one.
 *   start = 0: stop it (ms and periodic are 0).  Called for `stop T` and when
 *              the state that started the timer is left. */
typedef void (*fs_timer_fn)(fs_vm *vm, int timer, int start, int32_t ms, int periodic, void *user);

typedef struct {
    int32_t ret_pc;             /* -1 for the handler's base frame */
    int32_t base;               /* locals are stack[base + i]      */
} fs_frame;

typedef struct {
    fs_cell  args[FS_MAX_EVENT_ARGS];   /* int 0 beyond the event's argc */
    uint16_t event;
    uint8_t  types[FS_MAX_EVENT_ARGS];  /* type tag of each argument     */
} fs_qentry;

typedef struct {
    uint16_t len;
    uint8_t  used;
    uint8_t  mark;
    char     data[FS_STR_LEN + 1];      /* NUL terminated */
} fs_strslot;

typedef struct {
    uint8_t active;
    uint8_t periodic;
    int16_t owner;              /* state whose handler started it, -1 = global */
    int32_t period;
    int64_t remaining;
} fs_timer;

/*
 * The VM.  Defined here so the host can allocate it statically.  All fields
 * may be READ by a host/debugger; they must only be modified via the API.
 * Globals, stack slots and queued arguments are stored as fs_cell payloads
 * with their type tags in separate arrays: read them as values with
 * fs_global() and fs_stack_at().
 */
struct fs_vm {
    /* --- image (pointers into the caller's buffer, never copied) --- */
    const uint8_t *image;
    size_t         image_len;
    const uint8_t *strings;     /* start of the string section */
    const uint8_t *globals_init;
    const uint8_t *externs_tab; /* 8 bytes each */
    const uint8_t *events_tab;  /* 6 bytes each */
    const uint8_t *states_tab;  /* 2 bytes each */
    const uint8_t *timers_tab;  /* 2 bytes each */
    const uint8_t *funcs_tab;   /* 6 bytes each */
    const uint8_t *handlers_tab;/* 8 bytes each */
    const uint8_t *code;
    uint32_t code_size;
    uint16_t n_strings, n_globals, n_externs, n_events, n_states, n_timers,
             n_funcs, n_handlers;
    uint16_t prog_name;         /* string index or FS_NONE16 */
    uint16_t prog_version;
    uint8_t  loaded;

    /* --- host bindings --- */
    fs_extern_fn bound[FS_MAX_EXTERNS];
    fs_emit_fn   on_emit;
    fs_fault_fn  on_fault;
    fs_timer_fn  on_timer;      /* NULL: built-in timers driven by fs_tick() */
#if FS_HAS_TIME
    fs_clock_fn  clock;         /* NULL: now() is 0                          */
#endif
    void        *user;

    /* --- run-time state --- */
    fs_cell  globals[FS_MAX_GLOBALS];   /* payloads; type tags in gtypes   */
    fs_cell  stack[FS_STACK_SIZE];      /* payloads; type tags in stypes   */
    int      sp;                /* number of values on the stack.  May exceed
                                   FS_STACK_SIZE only when a handler declares
                                   more locals than that (as in vm.py); such
                                   virtual slots read as int 0 and every push
                                   faults with stack overflow. */
    fs_frame frames[FS_MAX_FRAMES];
    int      nframes;
    int32_t  pc;                /* next instruction                         */
    int32_t  cur_pc;            /* instruction being / last executed (-1)  */
    int      handler;           /* active handler index, -1 = none          */
    int      state;             /* current state, -1 = none                 */
    int      pending;           /* requested goto_state target, -1 = none         */
    int      transition;        /* state being entered, -1 = none           */
    uint8_t  in_exit;           /* running an exit handler (goto_state ignored)   */
    uint8_t  boot_pending;
    int32_t  steps;             /* instructions executed by this handler    */
    int      cur_extern;        /* extern being called, -1 = none           */
    uint32_t faults;            /* total number of faults reported          */
    int      chain;             /* dispatches since the VM was last idle    */
    fs_qentry queue[FS_QUEUE_SIZE];
    int      q_head, q_count;
    fs_timer timers[FS_MAX_TIMERS];
    uint8_t  gtypes[FS_MAX_GLOBALS];    /* type tag of each global          */
    uint8_t  stypes[FS_STACK_SIZE];     /* type tag of each stack slot      */
#if FS_HAS_STR
    fs_strslot strs[FS_STR_SLOTS];
#endif
};

/* ---- loading & binding ---------------------------------------------------- */
/* Validate and load an image; resets all run-time state and bindings (the
 * callbacks set by fs_set_callbacks are kept).  Returns FS_OK or FS_ERR_*. */
int  fs_load(fs_vm *vm, const uint8_t *image, size_t len);
/* Reset run-time state (globals to initial values, queue, timers, state);
 * keeps image, bindings and callbacks.  Call fs_start() afterwards. */
void fs_reset(fs_vm *vm);
/* Bind fn to every extern called `name`.  Returns the number of externs
 * bound (>= 1) or FS_ERR_NOTFOUND. */
int  fs_bind(fs_vm *vm, const char *name, fs_extern_fn fn);
/* Bind by extern index.  Returns FS_OK or FS_ERR_ARG. */
int  fs_bind_index(fs_vm *vm, int idx, fs_extern_fn fn);
/* Name of the first unbound extern, or NULL if all are bound.  Calling an
 * unbound extern faults with code 8. */
const char *fs_unbound(const fs_vm *vm);
void fs_set_callbacks(fs_vm *vm, fs_emit_fn on_emit, fs_fault_fn on_fault, void *user);

/* ---- running --------------------------------------------------------------- */
/* Queue `boot` and the entry into the initial state (first declared state). */
void fs_start(fs_vm *vm);
/* Queue an event.  args must have the event's parameter types (an int is
 * accepted for a float, fs_cstr(text) for a str); missing args are the zero
 * value of their type, extra args are ignored.  Returns FS_OK, FS_ERR_ARG
 * (unknown event, wrong type), FS_ERR_FULL (fault 7 is reported to the host
 * with pc -1, or the current pc inside a handler) or FS_ERR_MEM (no room for a
 * string argument: fault 11 or 12 reported the same way). */
int  fs_post(fs_vm *vm, int event, const fs_value *args, int argc);
/* The clock now() reads (NULL: now() is 0).  Kept across fs_load().
 * Does nothing in a FS_NO_TIME build. */
void fs_set_clock(fs_vm *vm, fs_clock_fn fn);
/* Advance all running timers by ms (SPEC 8.3); expired timers queue events. */
void fs_tick(fs_vm *vm, int32_t ms);    /* built-in timers only (no-op with a timer handler) */

/* Hand the timers to the host (NULL: back to built-in timers).  Call before
 * fs_start(); kept across fs_load().  The host calls fs_timer_expired() when a
 * timer it was asked to start fires; it re-arms periodic timers itself.
 * Returns FS_OK (timer event queued), FS_TIMER_STOPPED (the script stopped the
 * timer meanwhile: ignored), FS_ERR_FULL (fault 7 reported) or FS_ERR_ARG. */
void fs_set_timer_handler(fs_vm *vm, fs_timer_fn fn);
int  fs_timer_expired(fs_vm *vm, int timer);
int  fs_timer_count(const fs_vm *vm);
const char *fs_timer_name(const fs_vm *vm, int timer);  /* NULL if unknown */
/* One dispatch (no handler active) or one instruction.  Returns FS_IDLE,
 * FS_DISPATCHED, FS_RUNNING, FS_DONE or FS_FAULT. */
int  fs_step(fs_vm *vm);
/* fs_step() until idle or max_steps non-idle steps; returns the step count. */
int  fs_run(fs_vm *vm, int max_steps);

/* ---- names & lookup -------------------------------------------------------- */
/* String table entry (NUL terminated, inside the image) or NULL. */
const char *fs_string(const fs_vm *vm, int idx, uint16_t *len_out);
int  fs_event_id(const fs_vm *vm, const char *name);   /* -1 if unknown */
int  fs_state_id(const fs_vm *vm, const char *name);   /* -1 if unknown */
int  fs_extern_id(const fs_vm *vm, const char *name);  /* -1 if unknown */
int  fs_state(const fs_vm *vm);                        /* -1 = none     */
const char *fs_state_name(const fs_vm *vm, int idx);
const char *fs_event_name(const fs_vm *vm, int idx);
const char *fs_extern_name(const fs_vm *vm, int idx);
const char *fs_program_name(const fs_vm *vm);          /* NULL = none   */
int  fs_event_argc(const fs_vm *vm, int idx);          /* -1 if unknown */
int  fs_extern_argc(const fs_vm *vm, int idx);         /* -1 if unknown */
int  fs_event_arg_type(const fs_vm *vm, int idx, int arg);   /* FS_INT.., -1 */
int  fs_extern_arg_type(const fs_vm *vm, int idx, int arg);  /* FS_INT.., -1 */
int  fs_extern_ret(const fs_vm *vm, int idx);          /* type, FS_NONE_TYPE, -1 */

/* ---- values -------------------------------------------------------------------- */
/* The bytes of a str value (NUL terminated), or NULL if v is not a valid string.
 * Valid until the VM next allocates a string (copy it to keep it). */
const char *fs_str(const fs_vm *vm, fs_value v, uint16_t *len_out);
/* The empty string (no slot needed). */
fs_value fs_empty_str(void);
/* Canonical text of a value, as in the traces: 12, 2.5, "a\n", 2026-11-01T12:00:00.000Z
 * (an FS_CSTR is shown like a str).
 * Writes at most size-1 bytes + NUL; returns the full length. */
int  fs_format(const fs_vm *vm, fs_value v, char *buf, int size);
/* Text -> value with the VM's own rules (same as float("...") / time("...")).
 * Return 0 on success, -1 if the text is invalid. */
#if FS_HAS_FLOAT
int  fs_parse_float(const char *s, size_t len, double *out);
#endif
#if FS_HAS_TIME
int  fs_parse_time(const char *s, size_t len, int64_t *out);
#endif
int  fs_strings_in_use(const fs_vm *vm);
/* Index of the extern currently being called (valid inside fs_extern_fn). */
int  fs_current_extern(const fs_vm *vm);

/* ---- debugger read access (also available as struct fields) --------------- */
int     fs_active(const fs_vm *vm);           /* 1 while a handler runs  */
int     fs_handler(const fs_vm *vm);          /* handler index or -1     */
int32_t fs_pc(const fs_vm *vm);
int     fs_sp(const fs_vm *vm);
fs_value fs_stack_at(const fs_vm *vm, int i); /* int 0 if out of range   */
int     fs_global_count(const fs_vm *vm);
fs_value fs_global(const fs_vm *vm, int i);   /* int 0 if out of range   */
int     fs_frame_count(const fs_vm *vm);
const fs_frame *fs_frame_at(const fs_vm *vm, int i);  /* NULL if out of range */

#ifdef __cplusplus
}
#endif
#endif /* FLOWSCRIPT_H */
