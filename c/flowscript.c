/*
 * FlowScript virtual machine - see flowscript.h and SPEC.md.
 * Behaviour mirrors flowscript/vm.py exactly (fault codes, fault pcs, the
 * order in which checks are made, dispatch order).
 */
#include "flowscript.h"

#include <string.h>

/* ---- opcodes (flowscript/bytecode.py) -------------------------------------- */
enum {
    OP_NOP = 0x00, OP_PUSH = 0x01, OP_PUSH8 = 0x02, OP_PUSHS = 0x03, OP_POP = 0x04,
    OP_DUP = 0x05,
    OP_LOADG = 0x10, OP_STOREG = 0x11, OP_LOADL = 0x12, OP_STOREL = 0x13,
    OP_ADD = 0x20, OP_SUB = 0x21, OP_MUL = 0x22, OP_DIV = 0x23, OP_MOD = 0x24,
    OP_NEG = 0x25, OP_BAND = 0x26, OP_BOR = 0x27, OP_BXOR = 0x28, OP_BNOT = 0x29,
    OP_SHL = 0x2A, OP_SHR = 0x2B,
    OP_EQ = 0x30, OP_NE = 0x31, OP_LT = 0x32, OP_LE = 0x33, OP_GT = 0x34, OP_GE = 0x35,
    OP_NOT = 0x36, OP_BOOL = 0x37,
    OP_JMP = 0x40, OP_JZ = 0x41, OP_JNZ = 0x42,
    OP_CALL = 0x50, OP_RET = 0x51, OP_RET0 = 0x52, OP_XCALL = 0x53,
    OP_GOTO = 0x60, OP_EMIT = 0x61, OP_TSTART = 0x62, OP_TEVERY = 0x63, OP_TSTOP = 0x64
};

#define EV_BOOT  0
#define EV_ENTER 1
#define EV_EXIT  2

/* ---- byte access ------------------------------------------------------------- */
static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | ((unsigned)p[1] << 8));
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

/* uint32 -> int32 two's complement without implementation-defined behaviour */
static int32_t s32(uint32_t u)
{
    if (u & 0x80000000u)
        return -(int32_t)(~u) - 1;
    return (int32_t)u;
}

/* ---- image validation -------------------------------------------------------- */
/* Reserve `n` bytes at *off; returns pointer or NULL if out of bounds. */
static const uint8_t *take(const uint8_t *img, size_t len, size_t *off, size_t n)
{
    const uint8_t *p;
    if (*off > len || len - *off < n)
        return NULL;
    p = img + *off;
    *off += n;
    return p;
}

int fs_load(fs_vm *vm, const uint8_t *img, size_t len)
{
    fs_emit_fn on_emit;
    fs_fault_fn on_fault;
    fs_timer_fn on_timer;
    void *user;
    size_t off, n;
    uint16_t ns, flags;
    unsigned i;
    const uint8_t *p;

    if (!vm)
        return FS_ERR_ARG;
    on_emit = vm->on_emit;
    on_fault = vm->on_fault;
    on_timer = vm->on_timer;
    user = vm->user;
    memset(vm, 0, sizeof(*vm));
    vm->on_emit = on_emit;
    vm->on_fault = on_fault;
    vm->on_timer = on_timer;
    vm->user = user;
    vm->handler = -1;
    vm->state = vm->pending = vm->transition = -1;
    vm->cur_pc = -1;
    vm->cur_extern = -1;

    if (!img || len < 32)
        return img ? FS_ERR_MAGIC : FS_ERR_ARG;
    if (memcmp(img, "FSB1", 4) != 0)
        return FS_ERR_MAGIC;
    if (rd16(img + 4) != 1)
        return FS_ERR_VERSION;
    flags = rd16(img + 6);
    vm->prog_name = rd16(img + 8);
    vm->prog_version = rd16(img + 10);
    ns = vm->n_strings = rd16(img + 12);
    vm->n_globals = rd16(img + 14);
    vm->n_externs = rd16(img + 16);
    vm->n_events = rd16(img + 18);
    vm->n_states = rd16(img + 20);
    vm->n_timers = rd16(img + 22);
    vm->n_funcs = rd16(img + 24);
    vm->n_handlers = rd16(img + 26);
    vm->code_size = rd32(img + 28);

    if (vm->n_globals > FS_MAX_GLOBALS || vm->n_externs > FS_MAX_EXTERNS ||
        vm->n_timers > FS_MAX_TIMERS)
        return FS_ERR_LIMIT;
    if (vm->prog_name != FS_NONE16 && vm->prog_name >= ns)
        return FS_ERR_INDEX;

    off = 32;
    vm->strings = img + off;
    for (i = 0; i < ns; i++) {
        if (!(p = take(img, len, &off, 2)))
            return FS_ERR_TRUNCATED;
        n = rd16(p);
        if (!(p = take(img, len, &off, n + 1)))
            return FS_ERR_TRUNCATED;
        if (p[n] != 0)
            return FS_ERR_TRUNCATED;   /* missing NUL terminator */
    }
    if (!(vm->globals_init = take(img, len, &off, (size_t)vm->n_globals * 4)))
        return FS_ERR_TRUNCATED;
    if (!(vm->externs_tab = take(img, len, &off, (size_t)vm->n_externs * 4)))
        return FS_ERR_TRUNCATED;
    if (!(vm->events_tab = take(img, len, &off, (size_t)vm->n_events * 4)))
        return FS_ERR_TRUNCATED;
    if (!(vm->states_tab = take(img, len, &off, (size_t)vm->n_states * 2)))
        return FS_ERR_TRUNCATED;
    if (!(vm->timers_tab = take(img, len, &off, (size_t)vm->n_timers * 2)))
        return FS_ERR_TRUNCATED;
    if (!(vm->funcs_tab = take(img, len, &off, (size_t)vm->n_funcs * 6)))
        return FS_ERR_TRUNCATED;
    if (!(vm->handlers_tab = take(img, len, &off, (size_t)vm->n_handlers * 8)))
        return FS_ERR_TRUNCATED;
    if (vm->code_size > len || !(vm->code = take(img, len, &off, vm->code_size)))
        return FS_ERR_TRUNCATED;
    if (flags & 1u) {            /* debug section: not used, but must be sane */
        if (!(p = take(img, len, &off, 2)))
            return FS_ERR_TRUNCATED;
        if (!take(img, len, &off, (size_t)rd16(p) * 6))
            return FS_ERR_TRUNCATED;
        if (!(p = take(img, len, &off, (size_t)vm->n_globals * 2)))
            return FS_ERR_TRUNCATED;
        for (i = 0; i < vm->n_globals; i++)
            if (rd16(p + 2 * i) >= ns)
                return FS_ERR_INDEX;
    }

    for (i = 0; i < vm->n_externs; i++)
        if (rd16(vm->externs_tab + 4 * i) >= ns)
            return FS_ERR_INDEX;
    for (i = 0; i < vm->n_events; i++) {
        if (rd16(vm->events_tab + 4 * i) >= ns)
            return FS_ERR_INDEX;
        if (vm->events_tab[4 * i + 2] > FS_MAX_EVENT_ARGS)
            return FS_ERR_LIMIT;
    }
    for (i = 0; i < vm->n_states; i++)
        if (rd16(vm->states_tab + 2 * i) >= ns)
            return FS_ERR_INDEX;
    for (i = 0; i < vm->n_timers; i++)
        if (rd16(vm->timers_tab + 2 * i) >= vm->n_events)
            return FS_ERR_INDEX;
    for (i = 0; i < vm->n_funcs; i++) {
        p = vm->funcs_tab + 6 * i;
        if (rd16(p) >= ns)
            return FS_ERR_INDEX;
        if (rd16(p + 4) >= vm->code_size)
            return FS_ERR_OFFSET;
    }
    for (i = 0; i < vm->n_handlers; i++) {
        uint16_t st;
        p = vm->handlers_tab + 8 * i;
        st = rd16(p);
        if ((st != FS_NONE16 && st >= vm->n_states) || rd16(p + 2) >= vm->n_events)
            return FS_ERR_INDEX;
        if (rd16(p + 6) >= vm->code_size)
            return FS_ERR_OFFSET;
    }

    vm->image = img;
    vm->image_len = len;
    vm->loaded = 1;
    fs_reset(vm);
    return FS_OK;
}

void fs_reset(fs_vm *vm)
{
    unsigned i;
    if (!vm || !vm->loaded)
        return;
    for (i = 0; i < vm->n_globals; i++)
        vm->globals[i] = s32(rd32(vm->globals_init + 4 * i));
    vm->sp = 0;
    vm->nframes = 0;
    vm->pc = 0;
    vm->cur_pc = -1;
    vm->handler = -1;
    vm->state = vm->pending = vm->transition = -1;
    vm->in_exit = 0;
    vm->boot_pending = 0;
    vm->steps = 0;
    vm->cur_extern = -1;
    vm->faults = 0;
    vm->chain = 0;
    vm->q_head = vm->q_count = 0;
    for (i = 0; i < vm->n_timers; i++)
        if (vm->timers[i].active && vm->on_timer)
            vm->on_timer(vm, (int)i, 0, 0, 0, vm->user);
    memset(vm->timers, 0, sizeof(vm->timers));
}

/* ---- names ---------------------------------------------------------------------- */
const char *fs_string(const fs_vm *vm, int idx, uint16_t *len_out)
{
    const uint8_t *p;
    int i;
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_strings)
        return NULL;
    p = vm->strings;
    for (i = 0; i < idx; i++)
        p += 3 + rd16(p);
    if (len_out)
        *len_out = rd16(p);
    return (const char *)(p + 2);
}

static int name_eq(const fs_vm *vm, uint16_t sidx, const char *name)
{
    uint16_t n;
    const char *s = fs_string(vm, sidx, &n);
    return s && strlen(name) == n && memcmp(s, name, n) == 0;
}

static int find_name(const fs_vm *vm, const uint8_t *tab, unsigned stride, unsigned count,
                     const char *name)
{
    unsigned i;
    if (!vm || !vm->loaded || !name)
        return -1;
    for (i = 0; i < count; i++)
        if (name_eq(vm, rd16(tab + stride * i), name))
            return (int)i;
    return -1;
}

int fs_event_id(const fs_vm *vm, const char *name)
{
    return vm ? find_name(vm, vm->events_tab, 4, vm->n_events, name) : -1;
}

int fs_state_id(const fs_vm *vm, const char *name)
{
    return vm ? find_name(vm, vm->states_tab, 2, vm->n_states, name) : -1;
}

int fs_extern_id(const fs_vm *vm, const char *name)
{
    return vm ? find_name(vm, vm->externs_tab, 4, vm->n_externs, name) : -1;
}

const char *fs_state_name(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_states)
        return NULL;
    return fs_string(vm, rd16(vm->states_tab + 2 * idx), NULL);
}

const char *fs_event_name(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_events)
        return NULL;
    return fs_string(vm, rd16(vm->events_tab + 4 * idx), NULL);
}

const char *fs_extern_name(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return NULL;
    return fs_string(vm, rd16(vm->externs_tab + 4 * idx), NULL);
}

const char *fs_program_name(const fs_vm *vm)
{
    if (!vm || !vm->loaded || vm->prog_name == FS_NONE16)
        return NULL;
    return fs_string(vm, vm->prog_name, NULL);
}

int fs_event_argc(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_events)
        return -1;
    return vm->events_tab[4 * idx + 2];
}

int fs_extern_argc(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return -1;
    return vm->externs_tab[4 * idx + 2];
}

int fs_current_extern(const fs_vm *vm) { return vm ? vm->cur_extern : -1; }
int fs_state(const fs_vm *vm) { return vm ? vm->state : -1; }
int fs_active(const fs_vm *vm) { return vm && vm->handler >= 0; }
int fs_handler(const fs_vm *vm) { return vm ? vm->handler : -1; }
int32_t fs_pc(const fs_vm *vm) { return vm ? vm->pc : 0; }
int fs_sp(const fs_vm *vm) { return vm ? vm->sp : 0; }
int fs_global_count(const fs_vm *vm) { return vm && vm->loaded ? vm->n_globals : 0; }
int fs_frame_count(const fs_vm *vm) { return vm ? vm->nframes : 0; }

int32_t fs_stack_at(const fs_vm *vm, int i)
{
    if (!vm || i < 0 || i >= vm->sp || i >= FS_STACK_SIZE)
        return 0;
    return vm->stack[i];
}

int32_t fs_global(const fs_vm *vm, int i)
{
    if (!vm || !vm->loaded || i < 0 || i >= vm->n_globals)
        return 0;
    return vm->globals[i];
}

const fs_frame *fs_frame_at(const fs_vm *vm, int i)
{
    if (!vm || i < 0 || i >= vm->nframes)
        return NULL;
    return &vm->frames[i];
}

/* ---- binding --------------------------------------------------------------------- */
int fs_bind(fs_vm *vm, const char *name, fs_extern_fn fn)
{
    unsigned i;
    int n = 0;
    if (!vm || !vm->loaded || !name)
        return FS_ERR_ARG;
    for (i = 0; i < vm->n_externs; i++) {
        if (name_eq(vm, rd16(vm->externs_tab + 4 * i), name)) {
            vm->bound[i] = fn;
            n++;
        }
    }
    return n ? n : FS_ERR_NOTFOUND;
}

int fs_bind_index(fs_vm *vm, int idx, fs_extern_fn fn)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return FS_ERR_ARG;
    vm->bound[idx] = fn;
    return FS_OK;
}

const char *fs_unbound(const fs_vm *vm)
{
    unsigned i;
    if (!vm || !vm->loaded)
        return NULL;
    for (i = 0; i < vm->n_externs; i++)
        if (!vm->bound[i])
            return fs_extern_name(vm, (int)i);
    return NULL;
}

void fs_set_callbacks(fs_vm *vm, fs_emit_fn on_emit, fs_fault_fn on_fault, void *user)
{
    if (!vm)
        return;
    vm->on_emit = on_emit;
    vm->on_fault = on_fault;
    vm->user = user;
}

/* ---- host API ----------------------------------------------------------------------- */
static void report_fault(fs_vm *vm, int code, int pc)
{
    vm->faults++;
    if (vm->on_fault)
        vm->on_fault(vm, code, pc, vm->user);
}

void fs_start(fs_vm *vm)
{
    if (!vm || !vm->loaded)
        return;
    vm->boot_pending = 1;
    if (vm->n_states)
        vm->pending = 0;
}

int fs_post(fs_vm *vm, int event, const int32_t *args, int argc)
{
    fs_qentry *e;
    int i, n;
    if (!vm || !vm->loaded || event < 0 || event >= vm->n_events)
        return FS_ERR_ARG;
    if (vm->q_count >= FS_QUEUE_SIZE) {
        report_fault(vm, FS_FAULT_QUEUE_FULL, vm->handler >= 0 ? (int)vm->cur_pc : -1);
        return FS_ERR_FULL;
    }
    e = &vm->queue[(vm->q_head + vm->q_count) % FS_QUEUE_SIZE];
    vm->q_count++;
    e->event = (uint16_t)event;
    n = vm->events_tab[4 * event + 2];          /* <= FS_MAX_EVENT_ARGS */
    for (i = 0; i < FS_MAX_EVENT_ARGS; i++)
        e->args[i] = (i < n && args && i < argc) ? args[i] : 0;
    return FS_OK;
}

#define FS_REM_LIMIT ((int64_t)1 << 62)   /* keeps int64 math free of overflow */

static void timer_stop(fs_vm *vm, unsigned i)
{
    if (!vm->timers[i].active)
        return;
    vm->timers[i].active = 0;
    if (vm->on_timer)
        vm->on_timer(vm, (int)i, 0, 0, 0, vm->user);
}

void fs_set_timer_handler(fs_vm *vm, fs_timer_fn fn)
{
    if (vm)
        vm->on_timer = fn;
}

int fs_timer_count(const fs_vm *vm) { return vm && vm->loaded ? vm->n_timers : 0; }

const char *fs_timer_name(const fs_vm *vm, int timer)
{
    if (!vm || !vm->loaded || timer < 0 || timer >= vm->n_timers)
        return NULL;
    return fs_string(vm, rd16(vm->events_tab + 4 * rd16(vm->timers_tab + 2 * timer)), NULL);
}

int fs_timer_expired(fs_vm *vm, int timer)
{
    fs_timer *t;
    if (!vm || !vm->loaded || timer < 0 || timer >= vm->n_timers)
        return FS_ERR_ARG;
    t = &vm->timers[timer];
    if (!t->active)
        return FS_TIMER_STOPPED;
    if (!t->periodic)
        t->active = 0;
    return fs_post(vm, rd16(vm->timers_tab + 2 * timer), NULL, 0);
}

void fs_tick(fs_vm *vm, int32_t ms)
{
    unsigned i;
    if (!vm || !vm->loaded || vm->on_timer)
        return;
    for (i = 0; i < vm->n_timers; i++) {
        fs_timer *t = &vm->timers[i];
        if (!t->active)
            continue;
        t->remaining -= ms;
        if (t->remaining > FS_REM_LIMIT)
            t->remaining = FS_REM_LIMIT;
        if (t->remaining < -FS_REM_LIMIT)
            t->remaining = -FS_REM_LIMIT;
        if (t->remaining <= 0) {
            fs_post(vm, rd16(vm->timers_tab + 2 * i), NULL, 0);
            if (t->periodic) {
                t->remaining += t->period;
                if (t->remaining <= 0)
                    t->remaining = t->period;
            } else {
                t->active = 0;
            }
        }
    }
}

/* ---- dispatch -------------------------------------------------------------------------- */
/* Handler for (state, event): the state's own handler, else the first global
 * one, else -1.  state < 0 means "no state" (only global handlers match). */
static int find_handler(const fs_vm *vm, int state, int event)
{
    int glob = -1;
    unsigned i;
    for (i = 0; i < vm->n_handlers; i++) {
        const uint8_t *h = vm->handlers_tab + 8 * i;
        uint16_t st = rd16(h);
        if (rd16(h + 2) != event)
            continue;
        if (state >= 0 && st == state)
            return (int)i;
        if (st == FS_NONE16 && glob < 0)
            glob = (int)i;
    }
    return glob;
}

static int handler_state(const fs_vm *vm, int h)
{
    uint16_t st = rd16(vm->handlers_tab + 8 * h);
    return st == FS_NONE16 ? -1 : st;
}

static void begin(fs_vm *vm, int h, const int32_t *args)
{
    const uint8_t *p = vm->handlers_tab + 8 * h;
    int argc = p[4], nlocals = p[5], n, i;
    n = argc > nlocals ? argc : nlocals;
    for (i = 0; i < n && i < FS_STACK_SIZE; i++)
        vm->stack[i] = (args && i < argc && i < FS_MAX_EVENT_ARGS) ? args[i] : 0;
    vm->sp = n;       /* may exceed FS_STACK_SIZE: see fs_vm.sp */
    vm->handler = h;
    vm->frames[0].ret_pc = -1;
    vm->frames[0].base = 0;
    vm->nframes = 1;
    vm->pc = rd16(p + 6);
    vm->steps = 0;
}

static int dispatch(fs_vm *vm)
{
    int h;
    if (vm->boot_pending) {
        vm->boot_pending = 0;
        h = find_handler(vm, -1, EV_BOOT);
        if (h >= 0) {
            begin(vm, h, NULL);
            return 1;
        }
    }
    if (vm->pending >= 0) {
        vm->transition = vm->pending;
        vm->pending = -1;
        if (vm->state >= 0) {
            h = find_handler(vm, vm->state, EV_EXIT);
            if (h >= 0 && handler_state(vm, h) == vm->state) {
                vm->in_exit = 1;
                begin(vm, h, NULL);
                return 1;
            }
        }
    }
    if (vm->transition >= 0) {
        if (vm->state >= 0) {               /* leaving: stop the old state's timers */
            unsigned i;
            for (i = 0; i < vm->n_timers; i++)
                if (vm->timers[i].owner == vm->state)
                    timer_stop(vm, i);
        }
        vm->state = vm->transition;
        vm->transition = -1;
        h = find_handler(vm, vm->state, EV_ENTER);
        if (h >= 0 && handler_state(vm, h) == vm->state)
            begin(vm, h, NULL);
        return 1;
    }
    while (vm->q_count > 0) {
        const fs_qentry *e = &vm->queue[vm->q_head];
        vm->q_head = (vm->q_head + 1) % FS_QUEUE_SIZE;
        vm->q_count--;
        h = find_handler(vm, vm->state, e->event);
        if (h >= 0) {
            /* e stays valid: nothing is queued before begin() copies args */
            begin(vm, h, e->args);
            return 1;
        }
    }
    return 0;
}

static void finish(fs_vm *vm)
{
    vm->handler = -1;
    vm->nframes = 0;
    vm->sp = 0;
    vm->in_exit = 0;
}

/* ---- execution --------------------------------------------------------------------------- */
/* Stack slot access; slots >= FS_STACK_SIZE are virtual zeros (see fs_vm.sp). */
#define SLOT(i) ((i) < FS_STACK_SIZE ? vm->stack[(i)] : 0)

#define FAULT(c) do { fault = (c); goto faulted; } while (0)
#define POP(v) do { if (vm->sp <= 0) FAULT(FS_FAULT_STACK_UNDERFLOW); \
                    vm->sp--; (v) = SLOT(vm->sp); } while (0)
#define PUSH(v) do { int32_t pv_ = (v); if (vm->sp >= FS_STACK_SIZE) FAULT(FS_FAULT_STACK_OVERFLOW); \
                     vm->stack[vm->sp++] = pv_; } while (0)

/* Pop the top n values (caller checked sp >= n) and return a contiguous view
 * of them.  Values in virtual slots (see fs_vm.sp) are gathered into tmp
 * (FS_MAX_EVENT_ARGS entries); returns NULL if they do not fit there, which
 * is only possible for hand-crafted bytecode. */
static const int32_t *pop_args(fs_vm *vm, int n, int32_t *tmp)
{
    int base = vm->sp - n, i;
    vm->sp = base;
    if (base + n <= FS_STACK_SIZE)
        return &vm->stack[base];
    if (n > FS_MAX_EVENT_ARGS)
        return NULL;
    for (i = 0; i < n; i++)
        tmp[i] = SLOT(base + i);
    return tmp;
}

static int execute(fs_vm *vm)
{
    const uint8_t *code = vm->code;
    uint32_t csize = vm->code_size;
    int32_t pc = vm->pc, npc;
    unsigned arg = 0;
    int32_t a, b, v;
    int fault;
    uint8_t op;

    vm->cur_pc = pc;
    vm->steps++;
    if (vm->steps > FS_BUDGET)
        FAULT(FS_FAULT_BUDGET);
    if (pc < 0 || (uint32_t)pc >= csize)
        FAULT(FS_FAULT_BAD_OPCODE);
    op = code[pc];

    /* decode operand */
    switch (op) {
    case OP_NOP: case OP_POP: case OP_DUP:
    case OP_ADD: case OP_SUB: case OP_MUL: case OP_DIV: case OP_MOD: case OP_NEG:
    case OP_BAND: case OP_BOR: case OP_BXOR: case OP_BNOT: case OP_SHL: case OP_SHR:
    case OP_EQ: case OP_NE: case OP_LT: case OP_LE: case OP_GT: case OP_GE:
    case OP_NOT: case OP_BOOL: case OP_RET: case OP_RET0:
        npc = pc + 1;
        break;
    case OP_PUSH8: case OP_LOADL: case OP_STOREL:
        if ((uint32_t)pc + 2 > csize)
            FAULT(FS_FAULT_BAD_OPCODE);
        arg = code[pc + 1];
        npc = pc + 2;
        break;
    case OP_PUSH:
        if ((uint32_t)pc + 5 > csize)
            FAULT(FS_FAULT_BAD_OPCODE);
        arg = 0;
        npc = pc + 5;
        break;
    case OP_PUSHS: case OP_LOADG: case OP_STOREG: case OP_JMP: case OP_JZ: case OP_JNZ:
    case OP_CALL: case OP_XCALL: case OP_GOTO: case OP_EMIT:
    case OP_TSTART: case OP_TEVERY: case OP_TSTOP:
        if ((uint32_t)pc + 3 > csize)
            FAULT(FS_FAULT_BAD_OPCODE);
        arg = rd16(code + pc + 1);
        npc = pc + 3;
        break;
    default:
        FAULT(FS_FAULT_BAD_OPCODE);
    }
    vm->pc = npc;

    switch (op) {
    case OP_NOP:
        break;
    case OP_PUSH:
        PUSH(s32(rd32(code + pc + 1)));
        break;
    case OP_PUSH8:
        PUSH(arg < 0x80 ? (int32_t)arg : (int32_t)arg - 256);
        break;
    case OP_PUSHS:
        PUSH((int32_t)arg);
        break;
    case OP_POP:
        POP(v);
        (void)v;
        break;
    case OP_DUP:
        POP(v);
        PUSH(v);
        PUSH(v);
        break;
    case OP_LOADG:
        if (arg >= vm->n_globals)
            FAULT(FS_FAULT_BAD_OPCODE);
        PUSH(vm->globals[arg]);
        break;
    case OP_STOREG:
        if (arg >= vm->n_globals)
            FAULT(FS_FAULT_BAD_OPCODE);
        POP(v);
        vm->globals[arg] = v;
        break;
    case OP_LOADL: {
        int i = vm->frames[vm->nframes - 1].base + (int)arg;
        if (i >= vm->sp)
            FAULT(FS_FAULT_BAD_OPCODE);
        PUSH(SLOT(i));
        break;
    }
    case OP_STOREL: {
        int i;
        POP(v);
        i = vm->frames[vm->nframes - 1].base + (int)arg;
        if (i >= vm->sp)
            FAULT(FS_FAULT_BAD_OPCODE);
        if (i < FS_STACK_SIZE)
            vm->stack[i] = v;
        break;
    }
    case OP_ADD: case OP_SUB: case OP_MUL: case OP_DIV: case OP_MOD:
    case OP_BAND: case OP_BOR: case OP_BXOR: case OP_SHL: case OP_SHR:
    case OP_EQ: case OP_NE: case OP_LT: case OP_LE: case OP_GT: case OP_GE: {
        uint32_t ua, ub;
        POP(b);
        POP(a);
        ua = (uint32_t)a;
        ub = (uint32_t)b;
        switch (op) {
        case OP_ADD: v = s32(ua + ub); break;
        case OP_SUB: v = s32(ua - ub); break;
        case OP_MUL: v = s32(ua * ub); break;
        case OP_DIV:
            if (b == 0)
                FAULT(FS_FAULT_DIV_ZERO);
            v = (a == INT32_MIN && b == -1) ? INT32_MIN : a / b;
            break;
        case OP_MOD:
            if (b == 0)
                FAULT(FS_FAULT_DIV_ZERO);
            v = (b == -1) ? 0 : a % b;
            break;
        case OP_BAND: v = s32(ua & ub); break;
        case OP_BOR:  v = s32(ua | ub); break;
        case OP_BXOR: v = s32(ua ^ ub); break;
        case OP_SHL:  v = s32(ua << (ub & 31u)); break;
        case OP_SHR: {
            unsigned n = ub & 31u;
            /* arithmetic shift without relying on implementation-defined >> */
            v = a >= 0 ? (int32_t)((uint32_t)a >> n) : -(int32_t)((uint32_t)(-(a + 1)) >> n) - 1;
            break;
        }
        case OP_EQ: v = a == b; break;
        case OP_NE: v = a != b; break;
        case OP_LT: v = a < b; break;
        case OP_LE: v = a <= b; break;
        case OP_GT: v = a > b; break;
        default:    v = a >= b; break;
        }
        PUSH(v);
        break;
    }
    case OP_NEG:
        POP(a);
        PUSH(s32(0u - (uint32_t)a));
        break;
    case OP_BNOT:
        POP(a);
        PUSH(s32(~(uint32_t)a));
        break;
    case OP_NOT:
        POP(a);
        PUSH(a == 0);
        break;
    case OP_BOOL:
        POP(a);
        PUSH(a != 0);
        break;
    case OP_JMP:
        vm->pc = (int32_t)arg;   /* an out-of-range target faults on the next step */
        break;
    case OP_JZ:
        POP(v);
        if (v == 0)
            vm->pc = (int32_t)arg;
        break;
    case OP_JNZ:
        POP(v);
        if (v != 0)
            vm->pc = (int32_t)arg;
        break;
    case OP_CALL: {
        const uint8_t *f;
        int fargc, nlocals, base, i;
        if (arg >= vm->n_funcs)
            FAULT(FS_FAULT_BAD_OPCODE);
        f = vm->funcs_tab + 6 * arg;
        fargc = f[2];
        nlocals = f[3];
        if (vm->nframes >= FS_MAX_FRAMES)
            FAULT(FS_FAULT_CALL_DEPTH);
        base = vm->sp - fargc;
        if (base < 0)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        for (i = 0; i < nlocals - fargc; i++)
            PUSH(0);
        vm->frames[vm->nframes].ret_pc = npc;
        vm->frames[vm->nframes].base = base;
        vm->nframes++;
        vm->pc = rd16(f + 4);
        break;
    }
    case OP_RET:
    case OP_RET0: {
        const fs_frame *fr;
        v = 0;
        if (op == OP_RET)
            POP(v);
        fr = &vm->frames[--vm->nframes];
        if (vm->sp > fr->base)
            vm->sp = fr->base;
        if (vm->nframes == 0) {
            finish(vm);
            return FS_DONE;
        }
        vm->pc = fr->ret_pc;
        PUSH(v);
        break;
    }
    case OP_XCALL: {
        int32_t tmp[FS_MAX_EVENT_ARGS];
        const int32_t *args;
        int xargc;
        fs_extern_fn fn;
        if (arg >= vm->n_externs)
            FAULT(FS_FAULT_BAD_OPCODE);
        xargc = vm->externs_tab[4 * arg + 2];
        if (vm->sp < xargc)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        args = pop_args(vm, xargc, tmp);
        if (!args)                   /* only reachable with virtual slots */
            FAULT(FS_FAULT_STACK_OVERFLOW);
        fn = vm->bound[arg];
        if (!fn)
            FAULT(FS_FAULT_UNBOUND);
        vm->cur_extern = (int)arg;
        v = fn(vm, args, xargc, vm->user);
        vm->cur_extern = -1;
        PUSH(v);
        break;
    }
    case OP_GOTO:
        if (arg >= vm->n_states)
            FAULT(FS_FAULT_BAD_OPCODE);
        if (!vm->in_exit)
            vm->pending = (int)arg;
        finish(vm);
        return FS_DONE;
    case OP_EMIT: {
        int32_t tmp[FS_MAX_EVENT_ARGS], copy[FS_MAX_EVENT_ARGS];
        const int32_t *args;
        int eargc, i;
        if (arg >= vm->n_events)
            FAULT(FS_FAULT_BAD_OPCODE);
        eargc = vm->events_tab[4 * arg + 2];      /* <= FS_MAX_EVENT_ARGS */
        if (vm->sp < eargc)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        args = pop_args(vm, eargc, tmp);
        for (i = 0; i < eargc; i++)
            copy[i] = args[i];    /* the callback may not see stack slots change */
        if (vm->on_emit)
            vm->on_emit(vm, (int)arg, copy, eargc, vm->user);
        /* queue-full is reported but does not abort the handler (as vm.py) */
        fs_post(vm, (int)arg, copy, eargc);
        break;
    }
    case OP_TSTART:
    case OP_TEVERY: {
        fs_timer *t;
        if (arg >= vm->n_timers)
            FAULT(FS_FAULT_BAD_OPCODE);
        POP(v);
        t = &vm->timers[arg];
        t->active = 1;
        t->periodic = op == OP_TEVERY;
        t->period = v;
        t->remaining = v;
        t->owner = (int16_t)handler_state(vm, vm->handler);
        if (vm->on_timer)
            vm->on_timer(vm, (int)arg, 1, v, t->periodic, vm->user);
        break;
    }
    case OP_TSTOP:
        if (arg >= vm->n_timers)
            FAULT(FS_FAULT_BAD_OPCODE);
        timer_stop(vm, arg);
        break;
    default:
        FAULT(FS_FAULT_BAD_OPCODE);
    }
    return FS_RUNNING;

faulted:
    report_fault(vm, fault, (int)vm->cur_pc);
    finish(vm);
    /* pending is kept: only the initial entry queued by fs_start can be pending here */
    return FS_FAULT;
}

int fs_step(fs_vm *vm)
{
    if (!vm || !vm->loaded)
        return FS_IDLE;
    if (vm->handler < 0) {
        if (vm->chain >= FS_MAX_CHAIN && (vm->boot_pending || vm->pending >= 0 ||
                                          vm->transition >= 0 || vm->q_count > 0)) {
            /* the script keeps itself busy (goto_state/emit loop): drop the pending work */
            vm->boot_pending = 0;
            vm->pending = vm->transition = -1;
            vm->q_head = vm->q_count = 0;
            vm->chain = 0;
            report_fault(vm, FS_FAULT_CHAIN, -1);
            return FS_IDLE;
        }
        if (dispatch(vm)) {
            vm->chain++;
            return FS_DISPATCHED;
        }
        vm->chain = 0;
        return FS_IDLE;
    }
    return execute(vm);
}

int fs_run(fs_vm *vm, int max_steps)
{
    int n = 0;
    while (n < max_steps) {
        if (fs_step(vm) == FS_IDLE)
            break;
        n++;
    }
    return n;
}
