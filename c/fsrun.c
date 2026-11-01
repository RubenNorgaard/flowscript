/*
 * fsrun - run a FlowScript bytecode image against a scenario file and print
 * the canonical trace (byte-identical to flowscript/runner.py).
 *
 *   fsrun program.fsb [scenario]
 *
 * Every extern is bound to a stub that prints "call NAME(args) -> r" and
 * returns r (set with the scenario command "extern NAME VALUE", default: the
 * zero value of its type); "clock TIME" sets what now() returns.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "flowscript.h"

static fs_vm vm;
static fs_value stub_value[FS_MAX_EXTERNS];
static char stub_text[FS_MAX_EXTERNS][256];
static int stub_set[FS_MAX_EXTERNS];
static int64_t clock_ms;
static int last_state = -1;
static int prepass;            /* first pass over the scenario: only look for host_timers */
static int host_timers;

static void die(const char *msg, const char *arg)
{
    fflush(stdout);
    fprintf(stderr, "fsrun: %s%s%s\n", msg, arg ? ": " : "", arg ? arg : "");
    exit(1);
}

static char *read_file(const char *path, size_t *len_out)
{
    FILE *f = fopen(path, "rb");
    char *buf = NULL;
    size_t len = 0, cap = 0, n;
    if (!f)
        die("cannot open", path);
    for (;;) {
        if (cap - len < 4096) {
            char *nb;
            cap = cap ? cap * 2 : 65536;
            nb = realloc(buf, cap + 1);
            if (!nb)
                die("out of memory", NULL);
            buf = nb;
        }
        n = fread(buf + len, 1, cap - len, f);
        len += n;
        if (n == 0)
            break;
    }
    if (ferror(f))
        die("read error", path);
    fclose(f);
    buf[len] = 0;
    *len_out = len;
    return buf;
}

/* Python int(s, 0).  Returns 0 on success; *wrapped gets the value modulo
 * 2^32, *full the exact value (*full_ok = 0 if it does not fit in int64). */
static int parse_int(const char *s, int32_t *wrapped, int64_t *full, int *full_ok)
{
    int neg = 0, base = 10, ndig = 0, prev_us = 0, overflow = 0, all_zero = 1, first;
    uint64_t acc = 0;
    uint32_t w;
    if (*s == '+' || *s == '-')
        neg = *s++ == '-';
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) base = 16, s += 2;
    else if (s[0] == '0' && (s[1] == 'o' || s[1] == 'O')) base = 8, s += 2;
    else if (s[0] == '0' && (s[1] == 'b' || s[1] == 'B')) base = 2, s += 2;
    first = (base == 10) ? -1 : 0;
    if (base != 10 && *s == '_')       /* "0x_1f" is valid */
        s++, prev_us = 1;
    for (; *s; s++) {
        int d;
        if (*s == '_') {
            if (prev_us || ndig == 0)
                return -1;
            prev_us = 1;
            continue;
        }
        if (*s >= '0' && *s <= '9') d = *s - '0';
        else if (*s >= 'a' && *s <= 'z') d = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'Z') d = *s - 'A' + 10;
        else return -1;
        if (d >= base)
            return -1;
        if (first < 0)
            first = d;
        if (d)
            all_zero = 0;
        if (acc > (UINT64_MAX - (uint64_t)d) / (uint64_t)base)
            overflow = 1;
        acc = acc * (uint64_t)base + (uint64_t)d;
        ndig++;
        prev_us = 0;
    }
    if (ndig == 0 || prev_us)
        return -1;
    if (base == 10 && first == 0 && !all_zero)   /* "010" is invalid in Python */
        return -1;
    w = (uint32_t)acc;
    if (neg)
        w = 0u - w;
    *wrapped = (w & 0x80000000u) ? -(int32_t)(~w) - 1 : (int32_t)w;
    *full_ok = !overflow && acc <= (uint64_t)INT64_MAX;
    *full = *full_ok ? (neg ? -(int64_t)acc : (int64_t)acc) : 0;
    return 0;
}

static int32_t parse_wrapped(const char *s)
{
    int32_t w;
    int64_t f;
    int ok;
    if (parse_int(s, &w, &f, &ok) != 0)
        die("invalid integer", s);
    return w;
}

static int64_t parse_full(const char *s)
{
    int32_t w;
    int64_t f;
    int ok;
    if (parse_int(s, &w, &f, &ok) != 0)
        die("invalid integer", s);
    if (!ok)
        die("integer out of range", s);
    return f;
}

/* ---- callbacks ---------------------------------------------------------------- */
static void print_value(fs_value v)
{
    static char buf[4 * 65536 + 64];
    fs_format(&vm, v, buf, (int)sizeof(buf));
    fputs(buf, stdout);
}

static void print_args(const fs_value *args, int argc)
{
    int i;
    for (i = 0; i < argc; i++) {
        if (i)
            putchar(',');
        print_value(args[i]);
    }
}

static fs_value stub(fs_vm *v, const fs_value *args, int argc, void *user)
{
    int idx = fs_current_extern(v), ret = fs_extern_ret(v, idx);
    fs_value r;
    (void)user;
    printf("call %s(", fs_extern_name(v, idx));
    print_args(args, argc);
    if (ret == FS_NONE_TYPE) {
        printf(")\n");
        return fs_int(0);
    }
    if (!stub_set[idx])
        r = ret == FS_STR ? fs_empty_str() : ret == FS_FLOAT ? fs_float(0.0) : ret == FS_TIME ? fs_time(0) : fs_int(0);
    else
        r = stub_value[idx];
    printf(") -> ");
    print_value(r);
    putchar('\n');
    return r;
}

static void on_emit(fs_vm *v, int event, const fs_value *args, int argc, void *user)
{
    (void)user;
    printf("emit %s(", fs_event_name(v, event));
    print_args(args, argc);
    printf(")\n");
}

static int64_t on_clock(fs_vm *v, void *user)
{
    (void)v;
    (void)user;
    return clock_ms;
}

static void on_fault(fs_vm *v, int code, int pc, void *user)
{
    (void)v;
    (void)user;
    printf("fault %d at %d\n", code, pc);
}

static void on_timer(fs_vm *v, int timer, int start, int32_t ms, int periodic, void *user)
{
    (void)user;
    if (start)
        printf("timer %s %s %ld\n", periodic ? "every" : "start", fs_timer_name(v, timer), (long)ms);
    else
        printf("timer stop %s\n", fs_timer_name(v, timer));
}

/* ---- scenario ------------------------------------------------------------------ */
static void run(void)
{
    for (;;) {
        int r = fs_step(&vm);
        if (fs_state(&vm) != last_state) {
            last_state = fs_state(&vm);
            printf("state %s\n", fs_state_name(&vm, last_state));
        }
        if (r == FS_IDLE)
            break;
    }
}

static void dump(void)
{
    int i, n = fs_global_count(&vm);
    for (i = 0; i < n; i++) {
        printf("global[%d] = ", i);
        print_value(fs_global(&vm, i));
        putchar('\n');
    }
}

/* Python str.split()/strip() whitespace (ASCII subset) */
static int is_ws(int c)
{
    return c == ' ' || (c >= '\t' && c <= '\r') || (c >= 0x1c && c <= 0x1f);
}

/* Python str.splitlines() line breaks (ASCII subset) */
static int is_nl(int c)
{
    return c == '\n' || c == '\r' || c == '\v' || c == '\f' || (c >= 0x1c && c <= 0x1e);
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* values.unquote: "..." with \" \\ \n \t \xNN -> bytes in out (NUL terminated).
 * Returns the length, or -1 if text is not a valid quoted string. */
static int unquote(const char *text, char *out, int size)
{
    int n = (int)strlen(text), i, len = 0;
    if (n < 2 || text[0] != '"' || text[n - 1] != '"')
        return -1;
    for (i = 1; i < n - 1;) {
        int c = (unsigned char)text[i], v;
        if (c == '\\') {
            if (i + 1 >= n - 1)
                return -1;
            switch (text[i + 1]) {
            case 'x':
                if (i + 3 > n - 2 || hexval((unsigned char)text[i + 2]) < 0 ||
                    hexval((unsigned char)text[i + 3]) < 0)
                    return -1;
                v = hexval((unsigned char)text[i + 2]) * 16 + hexval((unsigned char)text[i + 3]);
                i += 4;
                break;
            case 'n': v = '\n'; i += 2; break;
            case 't': v = '\t'; i += 2; break;
            case '"': v = '"'; i += 2; break;
            case '\\': v = '\\'; i += 2; break;
            default: return -1;
            }
        } else {
            v = c;
            i++;
        }
        if (len >= size - 1)
            die("string too long in scenario", NULL);
        out[len++] = (char)v;
    }
    out[len] = 0;
    return len;
}

/* values.parse_literal: the text of a value of type t.  Strings go to buf. */
static fs_value parse_value(int t, const char *text, char *buf, int size)
{
    switch (t) {
    case FS_INT:
        return fs_int(parse_wrapped(text));
    case FS_FLOAT: {
        double f;
        if (fs_parse_float(text, strlen(text), &f))
            die("not a float", text);
        return fs_float(f);
    }
    case FS_STR:
        if (unquote(text, buf, size) < 0) {
            if ((int)strlen(text) >= size)
                die("string too long in scenario", NULL);
            strcpy(buf, text);
        }
        return fs_cstr(buf);
    default: {
        const char *d = text[0] == '-' ? text + 1 : text;
        int64_t ms;
        if (*d) {
            const char *p = d;
            while (*p >= '0' && *p <= '9')
                p++;
            if (!*p) {
                uint64_t acc = 0;
                for (p = d; *p; p++) {
                    if (acc > (UINT64_MAX - 9) / 10)
                        die("time out of range", text);
                    acc = acc * 10 + (uint64_t)(*p - '0');
                }
                return fs_time((int64_t)(text[0] == '-' ? 0u - acc : acc));
            }
        }
        if (fs_parse_time(text, strlen(text), &ms))
            die("not a time (use YYYY-MM-DDTHH:MM:SSZ or ms)", text);
        return fs_time(ms);
    }
    }
}

#define MAX_WORDS 64

/* runner.split_words: whitespace separated; a word starting with '"' runs to the
 * closing quote (backslash escapes); '#' outside quotes starts a comment. */
static int split_words(char *line, char **w)
{
    int starts[MAX_WORDS], ends[MAX_WORDS], n = 0, i = 0, len = (int)strlen(line), k;
    while (i < len) {
        int c = (unsigned char)line[i], j;
        if (is_ws(c)) {
            i++;
            continue;
        }
        if (c == '#')
            break;
        j = i;
        if (c == '"') {
            j++;
            while (j < len && line[j] != '"')
                j += line[j] == '\\' ? 2 : 1;
            j = j + 1 < len ? j + 1 : len;
        }
        while (j < len && !is_ws((unsigned char)line[j]) && line[j] != '#')
            j++;
        if (n == MAX_WORDS)
            die("too many words in scenario line", NULL);
        starts[n] = i;
        ends[n++] = j;
        i = j;
    }
    for (k = 0; k < n; k++) {
        line[ends[k]] = 0;          /* after the loop: the terminators don't change the split */
        w[k] = line + starts[k];
    }
    return n;
}

static void exec_line(char *line)
{
    char *w[MAX_WORDS];
    int n, i;
    n = split_words(line, w);
    if (n == 0)
        return;
    if (prepass) {
        if (strcmp(w[0], "host_timers") == 0)
            host_timers = 1;
        return;
    }
    if (strcmp(w[0], "host_timers") == 0) {
        /* handled in the first pass */
    } else if (strcmp(w[0], "fire") == 0) {
        int t;
        if (n < 2)
            die("fire needs a timer", NULL);
        for (t = 0; t < fs_timer_count(&vm); t++)
            if (strcmp(fs_timer_name(&vm, t), w[1]) == 0)
                break;
        if (t == fs_timer_count(&vm))
            die("unknown timer", w[1]);
        if (fs_timer_expired(&vm, t) == FS_TIMER_STOPPED)
            printf("fire %s ignored\n", w[1]);
        run();
    } else if (strcmp(w[0], "extern") == 0) {
        if (n < 3)
            die("extern needs NAME VALUE", NULL);
        for (i = 0; i < vm.n_externs; i++) {
            int t = fs_extern_ret(&vm, i);
            if (strcmp(fs_extern_name(&vm, i), w[1]) != 0 || t == FS_NONE_TYPE)
                continue;
            stub_value[i] = parse_value(t, w[2], stub_text[i], (int)sizeof(stub_text[i]));
            stub_set[i] = 1;
        }
    } else if (strcmp(w[0], "post") == 0) {
        static char bufs[FS_MAX_EVENT_ARGS][1024];
        fs_value args[FS_MAX_EVENT_ARGS];
        int ev, argc;
        if (n < 2)
            die("post needs an event", NULL);
        ev = fs_event_id(&vm, w[1]);
        if (ev < 0)
            die("unknown event", w[1]);
        argc = fs_event_argc(&vm, ev);
        if (n - 2 < argc)
            argc = n - 2;
        for (i = 0; i < argc; i++)
            args[i] = parse_value(fs_event_arg_type(&vm, ev, i), w[i + 2], bufs[i], (int)sizeof(bufs[i]));
        fs_post(&vm, ev, args, argc);
        run();
    } else if (strcmp(w[0], "tick") == 0) {
        int64_t ms;
        if (n < 2)
            die("tick needs a value", NULL);
        ms = parse_full(w[1]);
        if (ms < INT32_MIN || ms > INT32_MAX)
            die("tick value out of int32 range", w[1]);
        clock_ms = (int64_t)((uint64_t)clock_ms + (uint64_t)ms);
        fs_tick(&vm, (int32_t)ms);
        run();
    } else if (strcmp(w[0], "clock") == 0) {
        if (n < 2)
            die("clock needs a time", NULL);
        clock_ms = parse_value(FS_TIME, w[1], NULL, 0).as.t;
    } else if (strcmp(w[0], "dump") == 0) {
        dump();
    } else {
        die("unknown scenario command", w[0]);
    }
}

static void exec_scenario(char *sc, size_t slen)
{
    char *p = sc, *line;
    while (p < sc + slen) {
        line = p;
        while (p < sc + slen && !is_nl((unsigned char)*p))
            p++;
        if (p < sc + slen) {
            if (*p == '\r' && p + 1 < sc + slen && p[1] == '\n')
                *p++ = 0;
            *p++ = 0;
        }
        exec_line(line);
    }
}

int main(int argc, char **argv)
{
    size_t len, slen = 0;
    char *img, *sc = NULL;
    int rc, i;

    if (argc < 2 || argc > 3) {
        fprintf(stderr, "usage: fsrun program.fsb [scenario]\n");
        return 2;
    }
    img = read_file(argv[1], &len);
    rc = fs_load(&vm, (const uint8_t *)img, len);
    if (rc != FS_OK) {
        fprintf(stderr, "fsrun: %s: invalid bytecode image (error %d)\n", argv[1], rc);
        return 1;
    }
    for (i = 0; i < vm.n_externs; i++)
        fs_bind_index(&vm, i, stub);
    fs_set_callbacks(&vm, on_emit, on_fault, NULL);
    fs_set_clock(&vm, on_clock);
    if (argc == 3)
        sc = read_file(argv[2], &slen);
    if (sc) {
        char *copy = malloc(slen + 1);
        if (!copy)
            die("out of memory", NULL);
        memcpy(copy, sc, slen + 1);
        prepass = 1;
        exec_scenario(copy, slen);
        prepass = 0;
        free(copy);
    }
    if (host_timers)
        fs_set_timer_handler(&vm, on_timer);

    fs_start(&vm);
    run();
    if (sc)
        exec_scenario(sc, slen);
    dump();
    free(sc);
    free(img);
    return 0;
}
