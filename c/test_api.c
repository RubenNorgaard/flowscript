/*
 * API-level tests for paths the scenario runner cannot reach:
 *   test_api program.fsb      (compiled from tests/programs/api.fs)
 * - host fs_post / fs_tick into a full queue -> fault 7 at pc -1
 * - fs_post from inside an extern -> fault 7 at the XCALL pc
 * - unbound extern -> fault 8, fs_unbound()
 * - fs_post with an unknown event, zero-filled args
 * - fs_load rejects every truncation of the image and bad magic/version
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

static int32_t x_record(fs_vm *vm, const int32_t *args, int argc, void *user)
{
    int i;
    (void)vm; (void)user;
    n_calls++;
    for (i = 0; i < argc && i < 8; i++)
        last_args[i] = args[i];
    return 0;
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

static int32_t x_flood(fs_vm *vm, const int32_t *args, int argc, void *user)
{
    int i, ev = fs_event_id(vm, "noop");
    (void)args; (void)argc; (void)user;
    for (i = 0; i < FS_QUEUE_SIZE + 1; i++)
        fs_post(vm, ev, NULL, 0);
    return 0;
}

int main(int argc, char **argv)
{
    static fs_vm vm;
    static uint8_t img[65536], bad[65536];
    FILE *f;
    size_t len, cut;
    int i, ev_noop, ev_rec;
    int32_t a[2] = {11, 22};

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
    bad[4] = 2;
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

    /* fs_reset restores globals and clears state */
    fs_reset(&vm);
    CHECK(fs_state(&vm) == -1 && fs_global(&vm, 0) == 5);

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

    if (failures) {
        printf("test_api: %d failures\n", failures);
        return 1;
    }
    printf("test_api: all checks passed\n");
    return 0;
}
