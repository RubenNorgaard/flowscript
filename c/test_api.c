/*
 * API-level tests for paths the scenario runner cannot reach:
 *   test_api program.fsb      (compiled from tests/programs/api.fs)
 * - host fs_post / fs_tick into a full queue -> fault 7 at pc -1
 * - fs_post from inside an extern -> fault 7 at the XCALL pc
 * - unbound extern -> fault 8, fs_unbound()
 * - fs_post with an unknown event, zero-filled args
 * - fs_load rejects every truncation of the image and bad magic/version
 * - typed values: fs_post with C strings and int -> float, wrong types, a string
 *   too long (FS_ERR_MEM + fault 12), an extern returning the wrong type (fault 10),
 *   fs_format / fs_parse_float / fs_parse_time, the clock
 * - snapshots: fs_snapshot busy / too small / round trip, fs_restore errors
 * - recording: frames arrive in pieces with end = 1 last, lengths are right
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "flowscript.h"

static int failures;
#define CHECK(c) do { if (!(c)) { failures++; printf("FAIL line %d: %s\n", __LINE__, #c); } } while (0)

static int last_fault = -1, last_fault_pc = -2, n_faults, n_calls;
static int32_t last_args[8];

static void on_fault(fs_vm *vm, int code, int pc, void *user)
{
    (void)vm; (void)user;
    last_fault = code;
    last_fault_pc = pc;
    n_faults++;
}

static fs_value x_record(fs_vm *vm, const fs_value *args, int argc, void *user)
{
    int i;
    (void)vm; (void)user;
    n_calls++;
    for (i = 0; i < argc && i < 8; i++)
        last_args[i] = args[i].as.i;
    return fs_int(0);
}

static char echoed[128];
static fs_value x_echo(fs_vm *vm, const fs_value *args, int argc, void *user)
{
    uint16_t n = 0;
    const char *s = fs_str(vm, args[0], &n);
    (void)argc; (void)user;
    snprintf(echoed, sizeof(echoed), "%s?", s ? s : "(null)");
    return fs_cstr(echoed);              /* copied into a string slot by the VM */
}

static fs_value x_wrong(fs_vm *vm, const fs_value *args, int argc, void *user)
{
    (void)vm; (void)args; (void)argc; (void)user;
    return fs_cstr("not a float");
}

/* recording sink: frames may come in pieces; check each frame's length prefix */
static uint8_t rec_buf[4096];
static int rec_len, rec_frames, rec_bad, rec_frame_start;
static void x_record_sink(fs_vm *vm, const uint8_t *data, int len, int end, void *user)
{
    (void)vm; (void)user;
    if (rec_len + len > (int)sizeof(rec_buf)) {
        rec_bad++;
        return;
    }
    memcpy(rec_buf + rec_len, data, (size_t)len);
    rec_len += len;
    if (end) {                          /* the whole frame is in: varint length + that many bytes */
        int pos = rec_frame_start, shift = 0;
        unsigned n = 0;
        while (pos < rec_len) {
            unsigned b = rec_buf[pos++];
            n |= (b & 0x7Fu) << shift;
            shift += 7;
            if (!(b & 0x80u))
                break;
        }
        if ((int)(pos + n) != rec_len)
            rec_bad++;
        rec_frames++;
        rec_frame_start = rec_len;
    }
}

static int64_t x_clock(fs_vm *vm, void *user)
{
    (void)vm; (void)user;
    return 1793534400000LL;              /* 2026-11-01T12:00:00Z */
}

/* posts until the queue is full while a handler is running */
static int t_calls, t_last_timer = -1, t_last_start = -1, t_last_periodic = -1;
static int32_t t_last_ms = -1;
static void on_timer(fs_vm *vm, int timer, int start, int32_t ms, int periodic, void *user)
{
    (void)vm; (void)user;
    t_calls++;
    t_last_timer = timer;
    t_last_start = start;
    t_last_ms = ms;
    t_last_periodic = periodic;
}

static fs_value x_flood(fs_vm *vm, const fs_value *args, int argc, void *user)
{
    int i, ev = fs_event_id(vm, "noop");
    (void)args; (void)argc; (void)user;
    for (i = 0; i < FS_QUEUE_SIZE + 1; i++)
        fs_post(vm, ev, NULL, 0);
    return fs_int(0);
}

static int global_index(fs_vm *vm, const char *name)
{
    /* the test program's globals in declaration order: g, last, lastx */
    (void)vm;
    return strcmp(name, "g") == 0 ? 0 : strcmp(name, "last") == 0 ? 1 : 2;
}

int main(int argc, char **argv)
{
    static fs_vm vm;
    static uint8_t img[65536], bad[65536];
    FILE *f;
    size_t len, cut;
    int i, ev_noop, ev_rec;
    fs_value a[2];
    a[0] = fs_int(11);
    a[1] = fs_int(22);

    if (argc != 2 || !(f = fopen(argv[1], "rb"))) {
        fprintf(stderr, "usage: test_api api.fsb\n");
        return 2;
    }
    len = fread(img, 1, sizeof(img), f);
    fclose(f);

    /* every truncation must be rejected (image has no trailing slack) */
    for (cut = 0; cut < len; cut++) {
        memcpy(bad, img, cut);
        CHECK(fs_load(&vm, bad, cut) != FS_OK);
    }
    memcpy(bad, img, len);
    bad[0] = 'X';
    CHECK(fs_load(&vm, bad, len) == FS_ERR_MAGIC);
    memcpy(bad, img, len);
    bad[4] = 1;                                   /* FlowScript v1 images (untyped) */
    CHECK(fs_load(&vm, bad, len) == FS_ERR_VERSION);
    memcpy(bad, img, len);
    bad[16] = 0xFF;                               /* n_externs = 255 */
    CHECK(fs_load(&vm, bad, len) == FS_ERR_LIMIT);
    CHECK(fs_step(&vm) == FS_IDLE);               /* not loaded: harmless */

    fs_set_callbacks(&vm, NULL, on_fault, NULL);
    CHECK(fs_load(&vm, img, len) == FS_OK);
    CHECK(strcmp(fs_program_name(&vm), "api") == 0);
    CHECK(fs_unbound(&vm) && strcmp(fs_unbound(&vm), "record") == 0);
    CHECK(fs_bind(&vm, "record", x_record) == 1);
    CHECK(fs_bind(&vm, "nope", x_record) == FS_ERR_NOTFOUND);
    CHECK(fs_unbound(&vm) && strcmp(fs_unbound(&vm), "flood") == 0);
    CHECK(fs_bind(&vm, "echo", x_echo) == 1);
    CHECK(fs_bind(&vm, "wrong", x_wrong) == 1);
    CHECK(fs_extern_ret(&vm, fs_extern_id(&vm, "echo")) == FS_STR);
    CHECK(fs_extern_ret(&vm, fs_extern_id(&vm, "record")) == FS_INT);
    CHECK(fs_extern_arg_type(&vm, fs_extern_id(&vm, "echo"), 0) == FS_STR);
    CHECK(fs_extern_arg_type(&vm, fs_extern_id(&vm, "echo"), 1) == -1);

    ev_noop = fs_event_id(&vm, "noop");
    ev_rec = fs_event_id(&vm, "rec");
    CHECK(ev_noop > 2 && ev_rec > 2);
    CHECK(fs_event_id(&vm, "missing") == -1);
    CHECK(fs_post(&vm, 999, NULL, 0) == FS_ERR_ARG);
    CHECK(fs_post(&vm, -1, NULL, 0) == FS_ERR_ARG);

    fs_start(&vm);
    fs_run(&vm, 1000000);
    CHECK(n_faults == 0);

    /* missing args are zero filled */
    CHECK(fs_post(&vm, ev_rec, a, 1) == FS_OK);
    fs_run(&vm, 1000000);
    CHECK(n_calls == 1 && last_args[0] == 11 && last_args[1] == 0);
    CHECK(fs_post(&vm, ev_rec, a, 2) == FS_OK);
    fs_run(&vm, 1000000);
    CHECK(last_args[0] == 11 && last_args[1] == 22);

    /* host post into a full queue: fault 7 at pc -1 */
    for (i = 0; i < FS_QUEUE_SIZE; i++)
        CHECK(fs_post(&vm, ev_noop, NULL, 0) == FS_OK);
    CHECK(fs_post(&vm, ev_noop, NULL, 0) == FS_ERR_FULL);
    CHECK(last_fault == 7 && last_fault_pc == -1 && n_faults == 1);
    /* tick with a full queue: one-shot timer started by boot (10 ms) */
    fs_tick(&vm, 10);
    CHECK(n_faults == 2 && last_fault == 7 && last_fault_pc == -1);
    fs_run(&vm, 1000000);
    CHECK(vm.q_count == 0);

    /* unbound extern: fault 8 at the XCALL */
    CHECK(fs_post(&vm, fs_event_id(&vm, "doflood"), NULL, 0) == FS_OK);
    fs_run(&vm, 1000000);
    CHECK(n_faults == 3 && last_fault == 8 && last_fault_pc >= 0);

    /* post from inside an extern: fault 7 reported at the XCALL pc */
    CHECK(fs_bind(&vm, "flood", x_flood) == 1);
    CHECK(fs_unbound(&vm) == NULL);
    {
        int xpc = last_fault_pc;
        CHECK(fs_post(&vm, fs_event_id(&vm, "doflood"), NULL, 0) == FS_OK);
        fs_run(&vm, 1000000);
        CHECK(n_faults == 4 && last_fault == 7 && last_fault_pc == xpc);
    }

    /* typed values ------------------------------------------------------------------- */
    {
        int ev_text = fs_event_id(&vm, "text"), faults0;
        fs_value t[2];
        char buf[64];
        double f;
        int64_t ms;
        uint16_t n;
        CHECK(fs_event_arg_type(&vm, ev_text, 0) == FS_STR && fs_event_arg_type(&vm, ev_text, 1) == FS_FLOAT);
        t[0] = fs_cstr("hi");
        t[1] = fs_int(3);                             /* int accepted for a float */
        CHECK(fs_post(&vm, ev_text, t, 2) == FS_OK);
        CHECK(fs_strings_in_use(&vm) == 1);           /* "hi" copied into a slot */
        fs_run(&vm, 100000);
        CHECK(strcmp(echoed, "hi!?") == 0);
        CHECK(fs_global(&vm, global_index(&vm, "lastx")).type == FS_FLOAT &&
              fs_global(&vm, global_index(&vm, "lastx")).as.f == 3.0);
        CHECK(strcmp(fs_str(&vm, fs_global(&vm, global_index(&vm, "last")), &n), "hi!?") == 0 && n == 4);
        CHECK(fs_format(&vm, fs_global(&vm, 1), buf, sizeof(buf)) == 6 && strcmp(buf, "\"hi!?\"") == 0);
        CHECK(fs_format(&vm, fs_float(0.1), buf, sizeof(buf)) == 3 && strcmp(buf, "0.1") == 0);
        CHECK(fs_format(&vm, fs_time(0), buf, 8) == 24 && strcmp(buf, "1970-01") == 0);   /* truncated */
        CHECK(fs_format(&vm, fs_int(INT32_MIN), buf, sizeof(buf)) == 11 && strcmp(buf, "-2147483648") == 0);

        t[1] = fs_cstr("3");                          /* a str for a float: refused */
        CHECK(fs_post(&vm, ev_text, t, 2) == FS_ERR_ARG);
        CHECK(vm.q_count == 0);
        t[0] = fs_cstr("0123456789012345678901234567890123456789012345678901234567890123456789");
        t[1] = fs_float(1.5);
        faults0 = n_faults;
        CHECK(fs_post(&vm, ev_text, t, 2) == FS_ERR_MEM);
        CHECK(n_faults == faults0 + 1 && last_fault == FS_FAULT_STR_LONG && last_fault_pc == -1);
        CHECK(vm.q_count == 0);
        CHECK(fs_post(&vm, ev_text, NULL, 0) == FS_OK);   /* missing: "" and 0.0, no slot */
        fs_run(&vm, 100000);
        CHECK(strcmp(echoed, "!?") == 0);

        /* an extern returning the wrong type: fault 10 at the XCALL */
        CHECK(fs_post(&vm, fs_event_id(&vm, "mistype"), NULL, 0) == FS_OK);
        fs_run(&vm, 100000);
        CHECK(n_faults == faults0 + 2 && last_fault == FS_FAULT_TYPE && last_fault_pc >= 0);

        CHECK(fs_parse_float("2.5e3", 5, &f) == 0 && f == 2500.0);
        CHECK(fs_parse_float("2.5x", 4, &f) == -1);
        CHECK(fs_parse_time("2026-11-01T12:00:00Z", 20, &ms) == 0 && ms == 1793534400000LL);
        CHECK(fs_parse_time("2026-11-31", 10, &ms) == -1);
        CHECK(fs_str(&vm, fs_int(1), NULL) == NULL);
        CHECK(fs_str(&vm, fs_empty_str(), &n) && n == 0);
        fs_set_clock(&vm, x_clock);
        CHECK(vm.clock == x_clock);
    }

    /* fs_reset restores globals and clears state */
    fs_reset(&vm);
    CHECK(fs_state(&vm) == -1 && fs_global(&vm, 0).as.i == 5 && fs_strings_in_use(&vm) == 0);

    /* host-managed timers (api.fs: on boot: start t 10) */
    CHECK(fs_timer_count(&vm) == 1 && strcmp(fs_timer_name(&vm, 0), "t") == 0);
    CHECK(fs_timer_name(&vm, 1) == NULL);
    fs_set_timer_handler(&vm, on_timer);
    fs_start(&vm);
    fs_run(&vm, 100000);
    CHECK(t_calls == 1 && t_last_timer == 0 && t_last_start == 1 && t_last_ms == 10 && t_last_periodic == 0);
    fs_tick(&vm, 1000);                               /* no-op with a timer handler */
    CHECK(vm.q_count == 0 && vm.timers[0].active);
    CHECK(fs_timer_expired(&vm, 5) == FS_ERR_ARG);
    CHECK(fs_timer_expired(&vm, 0) == FS_OK);         /* one-shot: queued, now stopped */
    CHECK(vm.q_count == 1 && !vm.timers[0].active);
    CHECK(fs_timer_expired(&vm, 0) == FS_TIMER_STOPPED);
    fs_run(&vm, 100000);
    fs_reset(&vm);
    fs_start(&vm);
    fs_run(&vm, 100000);                              /* started again */
    t_calls = 0;
    fs_reset(&vm);                                    /* active timer: host is told to stop it */
    CHECK(t_calls == 1 && t_last_start == 0 && t_last_timer == 0);
    fs_set_timer_handler(&vm, NULL);

    /* ---- snapshots ---- */
    {
        int ev_text = fs_event_id(&vm, "text");
        uint8_t snap[512], small[8];
        int n, m;
        fs_value a[2];
        fs_reset(&vm);
        fs_start(&vm);
        CHECK(fs_snapshot(&vm, snap, sizeof(snap)) == FS_ERR_BUSY);   /* boot is pending */
        fs_run(&vm, 100000);
        a[0] = fs_cstr("snap");
        a[1] = fs_float(2.5);
        CHECK(fs_post(&vm, ev_text, a, 2) == FS_OK);
        fs_run(&vm, 100000);
        n = fs_snapshot(&vm, NULL, 0);                                /* the size it needs */
        CHECK(n > 14 && n < (int)sizeof(snap));
        CHECK(fs_snapshot(&vm, small, sizeof(small)) == n);           /* too small: nothing written */
        CHECK(fs_snapshot(&vm, snap, sizeof(snap)) == n);
        CHECK(memcmp(snap, "FSNP", 4) == 0);
        fs_reset(&vm);
        CHECK(fs_restore(&vm, snap, (size_t)n) == FS_OK);
        {
            uint16_t sl = 0;
            const char *t = fs_str(&vm, fs_global(&vm, 1), &sl);      /* last = echo("snap!") */
            CHECK(t && strcmp(t, "snap!?") == 0 && fs_global(&vm, 2).as.f == 2.5);
        }
        m = fs_snapshot(&vm, small, 0);
        CHECK(m == n);                                                /* the same state again */
        CHECK(fs_restore(&vm, snap, (size_t)n - 1) == FS_ERR_TRUNCATED);
        snap[6] ^= 1;
        CHECK(fs_restore(&vm, snap, (size_t)n) == FS_ERR_IMAGE);
        snap[6] ^= 1;
        snap[0] = 'X';
        CHECK(fs_restore(&vm, snap, (size_t)n) == FS_ERR_MAGIC);
        snap[0] = 'F';
        CHECK(fs_restore(&vm, snap, (size_t)n) == FS_OK);
    }

    /* ---- recording ---- */
    {
        int ev_text = fs_event_id(&vm, "text"), ev_noop = fs_event_id(&vm, "noop");
        fs_value a[2];
        fs_set_record_sink(&vm, x_record_sink);
        fs_reset(&vm);
        fs_start(&vm);
        fs_run(&vm, 100000);
        a[0] = fs_cstr("a text long enough for pieces of 32 bytes!");
        a[1] = fs_int(3);                                             /* an int for a float */
        CHECK(fs_post(&vm, ev_text, a, 2) == FS_OK);
        fs_run(&vm, 100000);
        CHECK(fs_record_keyframe(&vm) == FS_OK);
        fs_post(&vm, ev_noop, NULL, 0);
        CHECK(fs_record_keyframe(&vm) == FS_ERR_BUSY);               /* an event is waiting */
        fs_run(&vm, 100000);
        fs_record_flush(&vm);
        /* reset, start, steps, post, steps(+result), keyframe, post, steps */
        CHECK(rec_bad == 0 && rec_frames >= 7 && rec_len > 100);
        fs_set_record_sink(&vm, NULL);
    }

    if (failures) {
        printf("test_api: %d failures\n", failures);
        return 1;
    }
    printf("test_api: all checks passed\n");
    return 0;
}
