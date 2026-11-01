/*
 * FlowScript virtual machine - see flowscript.h and SPEC.md.
 * Behaviour mirrors flowscript/vm.py exactly (fault codes, fault pcs, the
 * order in which checks are made, dispatch order).
 */
#include "flowscript.h"

#if FS_HAS_FLOAT
#include <math.h>
#endif
#include <string.h>

/* ---- opcodes (flowscript/bytecode.py) -------------------------------------- */
enum {
    OP_NOP = 0x00, OP_PUSH = 0x01, OP_PUSH8 = 0x02, OP_PUSHS = 0x03, OP_POP = 0x04,
    OP_DUP = 0x05, OP_PUSHF = 0x06, OP_PUSHT = 0x07,
    OP_LOADG = 0x10, OP_STOREG = 0x11, OP_LOADL = 0x12, OP_STOREL = 0x13,
    OP_ADD = 0x20, OP_SUB = 0x21, OP_MUL = 0x22, OP_DIV = 0x23, OP_MOD = 0x24,
    OP_NEG = 0x25, OP_BAND = 0x26, OP_BOR = 0x27, OP_BXOR = 0x28, OP_BNOT = 0x29,
    OP_SHL = 0x2A, OP_SHR = 0x2B, OP_CONCAT = 0x2C,
    OP_EQ = 0x30, OP_NE = 0x31, OP_LT = 0x32, OP_LE = 0x33, OP_GT = 0x34, OP_GE = 0x35,
    OP_NOT = 0x36, OP_BOOL = 0x37,
    OP_JMP = 0x40, OP_JZ = 0x41, OP_JNZ = 0x42,
    OP_CALL = 0x50, OP_RET = 0x51, OP_RET0 = 0x52, OP_XCALL = 0x53, OP_BUILTIN = 0x54,
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

static uint64_t rd64(const uint8_t *p)
{
    return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

/* uint32 -> int32 two's complement without implementation-defined behaviour */
static int32_t s32(uint32_t u)
{
    if (u & 0x80000000u)
        return -(int32_t)(~u) - 1;
    return (int32_t)u;
}

#if FS_HAS_TIME
static int64_t s64(uint64_t u)
{
    if (u & 0x8000000000000000u)
        return -(int64_t)(~u) - 1;
    return (int64_t)u;
}
#endif

#if FS_HAS_FLOAT
static double f64(uint64_t u)
{
    double d;
    memcpy(&d, &u, sizeof(d));
    return d;
}
#endif

#define EMPTY_STR INT32_MIN     /* fs_value.as.s of the empty string constant */

static fs_value v_str(int32_t ref) { fs_value v = fs_int(0); v.type = FS_STR; v.as.s = ref; return v; }

static fs_value zero_of(int type)
{
    switch (type) {
#if FS_HAS_FLOAT
    case FS_FLOAT: return fs_float(0.0);
#endif
    case FS_STR:   return v_str(EMPTY_STR);
#if FS_HAS_TIME
    case FS_TIME:  return fs_time(0);
#endif
    default:       return fs_int(0);
    }
}

/* ---- stored values: payload cell + separate type tag ------------------------- */
static fs_value cell_value(uint8_t type, fs_cell c)
{
    fs_value v = fs_int(0);
    v.type = type;
    memcpy(&v.as, &c, sizeof(c));       /* every cell member is also in fs_value.as */
    return v;
}

static fs_cell value_cell(fs_value v)
{
    fs_cell c;
    memcpy(&c, &v.as, sizeof(c));
    return c;
}

static fs_value gget(const fs_vm *vm, int i) { return cell_value(vm->gtypes[i], vm->globals[i]); }
static void gset(fs_vm *vm, int i, fs_value v) { vm->gtypes[i] = v.type; vm->globals[i] = value_cell(v); }
static void sset(fs_vm *vm, int i, fs_value v) { vm->stypes[i] = v.type; vm->stack[i] = value_cell(v); }
static fs_value qget(const fs_qentry *e, int j) { return cell_value(e->types[j], e->args[j]); }
static void qset(fs_qentry *e, int j, fs_value v) { e->types[j] = v.type; e->args[j] = value_cell(v); }

/* parameter type i (2 bits each) */
static int ptype(uint32_t packed, int i) { return (int)((packed >> (2 * i)) & 3u); }

/* ---- value rules (flowscript/values.py: same arithmetic, same text) ----------- */
#if FS_HAS_FLOAT
static const double P10[23] = {
    1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15,
    1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22
};

static double scale_up(double m, int k)
{
    while (k > 22) {
        m = m * 1e22;
        k -= 22;
    }
    return m * P10[k];
}

static double scale_down(double m, int k)
{
    while (k > 22) {
        m = m / 1e22;
        k -= 22;
    }
    return m / P10[k];
}

static int dec_exp(double a)
{
    int d = 0;
    if (a >= 1.0) {
        while (d < 400 && scale_up(1.0, d + 1) <= a)
            d++;
    } else {
        while (d > -400 && scale_up(a, -d) < 1.0)
            d--;
    }
    return d;
}

static int64_t digits15(double a, int d)
{
    int s = 14 - d;
    double m = s >= 0 ? scale_up(a, s) : scale_down(a, -s);
    return (int64_t)(m + 0.5);
}

#endif /* FS_HAS_FLOAT */

/* Python str(int) */
static int fmt_i64(int64_t v, char *out)
{
    char tmp[24];
    int n = 0, len = 0;
#if FS_HAS_TIME || FS_HAS_FLOAT
    /* times, and the 15-digit mantissas of fmt_float */
    uint64_t u = v < 0 ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
#else
    /* only int32 values: 32-bit division, no 64-bit runtime helper */
    uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v;
#endif
    do {
        tmp[n++] = (char)('0' + (int)(u % 10u));
        u /= 10u;
    } while (u);
    if (v < 0)
        out[len++] = '-';
    while (n)
        out[len++] = tmp[--n];
    out[len] = 0;
    return len;
}

#if FS_HAS_FLOAT
/* values.fmt_float; out needs 32 bytes */
static int fmt_float(double x, char *out)
{
    char dg[24], *o = out;
    int d, nd, i, neg;
    int64_t n;
    const int64_t E15 = 1000000000000000LL, E14 = 100000000000000LL;
    double a;
    if (x != x) {
        memcpy(out, "nan", 4);
        return 3;
    }
    if (x == INFINITY || x == -INFINITY) {
        memcpy(out, x > 0 ? "inf" : "-inf", x > 0 ? 4 : 5);
        return x > 0 ? 3 : 4;
    }
    if (x == 0.0) {
        if (signbit(x)) {
            memcpy(out, "-0.0", 5);
            return 4;
        }
        memcpy(out, "0.0", 4);
        return 3;
    }
    neg = x < 0;
    a = neg ? -x : x;
    d = dec_exp(a);
    n = digits15(a, d);
    if (n >= E15) {
        n = (n + 5) / 10;
        d++;
    } else if (n < E14) {
        d--;
        n = digits15(a, d);
        if (n >= E15) {
            n = (n + 5) / 10;
            d++;
        }
    }
    nd = fmt_i64(n, dg);
    while (nd > 1 && dg[nd - 1] == '0')
        nd--;
    if (neg)
        *o++ = '-';
    if (d >= -4 && d < 15) {
        if (d >= 0) {
            for (i = 0; i <= d; i++)
                *o++ = i < nd ? dg[i] : '0';
            *o++ = '.';
            if (nd > d + 1)
                for (i = d + 1; i < nd; i++)
                    *o++ = dg[i];
            else
                *o++ = '0';
        } else {
            *o++ = '0';
            *o++ = '.';
            for (i = 0; i < -d - 1; i++)
                *o++ = '0';
            for (i = 0; i < nd; i++)
                *o++ = dg[i];
        }
    } else {
        int ad = d < 0 ? -d : d;
        *o++ = dg[0];
        if (nd > 1) {
            *o++ = '.';
            for (i = 1; i < nd; i++)
                *o++ = dg[i];
        }
        *o++ = 'e';
        *o++ = d < 0 ? '-' : '+';
        if (ad < 10)
            *o++ = '0';
        o += fmt_i64(ad, o);
    }
    *o = 0;
    return (int)(o - out);
}

int fs_parse_float(const char *s, size_t n, double *out)
{
    size_t i = 0;
    int neg = 0, ndig = 0, seen = 0;
    long exp = 0;
    uint64_t mant = 0;
    double v;
    if (!s || !out)
        return -1;
    if (i < n && (s[i] == '+' || s[i] == '-'))
        neg = s[i++] == '-';
    if (n - i == 3 && memcmp(s + i, "nan", 3) == 0) {
        *out = NAN;
        return 0;
    }
    if (n - i == 3 && memcmp(s + i, "inf", 3) == 0) {
        *out = neg ? -INFINITY : INFINITY;
        return 0;
    }
    while (i < n && s[i] >= '0' && s[i] <= '9') {
        seen++;
        if (ndig < 18) {
            mant = mant * 10u + (uint64_t)(s[i] - '0');
            if (mant)
                ndig++;
        } else {
            exp++;
        }
        i++;
    }
    if (i < n && s[i] == '.') {
        i++;
        while (i < n && s[i] >= '0' && s[i] <= '9') {
            seen++;
            if (ndig < 18) {
                mant = mant * 10u + (uint64_t)(s[i] - '0');
                if (mant)
                    ndig++;
                exp--;
            }
            i++;
        }
    }
    if (!seen)
        return -1;
    if (i < n && (s[i] == 'e' || s[i] == 'E')) {
        int eneg = 0;
        long e = 0;
        i++;
        if (i < n && (s[i] == '+' || s[i] == '-'))
            eneg = s[i++] == '-';
        if (i >= n || s[i] < '0' || s[i] > '9')
            return -1;
        while (i < n && s[i] >= '0' && s[i] <= '9') {
            if (e < 100000)
                e = e * 10 + (s[i] - '0');
            i++;
        }
        exp += eneg ? -e : e;
    }
    if (i != n)
        return -1;
    v = (double)mant;
    if (mant) {
        if (exp > 0)
            v = scale_up(v, exp > 400 ? 400 : (int)exp);
        else if (exp < 0)
            v = scale_down(v, -exp > 400 ? 400 : (int)-exp);
    }
    *out = neg ? -v : v;
    return 0;
}

#endif /* FS_HAS_FLOAT */

/* values.parse_int: [+-]digits within int32 */
static int parse_i32(const char *s, size_t n, int32_t *out)
{
    size_t i = 0;
    int neg = 0;
    int64_t v = 0;
    if (i < n && (s[i] == '+' || s[i] == '-'))
        neg = s[i++] == '-';
    if (i >= n)
        return -1;
    for (; i < n; i++) {
        if (s[i] < '0' || s[i] > '9')
            return -1;
        v = v * 10 + (s[i] - '0');
        if (v > 2147483648LL)
            return -1;
    }
    if (neg)
        v = -v;
    if (v > INT32_MAX || v < INT32_MIN)
        return -1;
    *out = (int32_t)v;
    return 0;
}

#if FS_HAS_TIME
#define DAY_MS 86400000LL

static int64_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0)))
        q--;
    return q;
}

static int64_t days_from_civil(int64_t y, int64_t m, int64_t d)
{
    int64_t era, yoe, doy, doe;
    y -= m <= 2;
    era = floor_div(y, 400);
    yoe = y - era * 400;
    doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static void civil_from_days(int64_t z, int64_t *y, int64_t *m, int64_t *d)
{
    int64_t era, doe, yoe, doy, mp;
    z += 719468;
    era = floor_div(z, 146097);
    doe = z - era * 146097;
    yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    mp = (5 * doy + 2) / 153;
    *d = doy - (153 * mp + 2) / 5 + 1;
    *m = mp + (mp < 10 ? 3 : -9);
    *y = yoe + era * 400 + (*m <= 2);
}

static int days_in_month(int64_t y, int64_t m)
{
    if (m == 2)
        return ((y % 4 == 0 && y % 100 != 0) || y % 400 == 0) ? 29 : 28;
    return (m == 4 || m == 6 || m == 9 || m == 11) ? 30 : 31;
}

static int make_time(int64_t y, int64_t mo, int64_t d, int64_t h, int64_t mi, int64_t s, int64_t *out)
{
    if (!(y >= 1 && y <= 9999 && mo >= 1 && mo <= 12 && d >= 1 && d <= days_in_month(y, mo) &&
          h >= 0 && h <= 23 && mi >= 0 && mi <= 59 && s >= 0 && s <= 59))
        return -1;
    *out = days_from_civil(y, mo, d) * DAY_MS + ((h * 60 + mi) * 60 + s) * 1000;
    return 0;
}

/* year, month, day, hour, minute, second, millisecond, weekday (0 = Monday) */
static void split_time(int64_t ms, int64_t f[8])
{
    int64_t days = floor_div(ms, DAY_MS), rem = ms - days * DAY_MS, wd;
    civil_from_days(days, &f[0], &f[1], &f[2]);
    f[3] = rem / 3600000;
    f[4] = rem / 60000 % 60;
    f[5] = rem / 1000 % 60;
    f[6] = rem % 1000;
    wd = (days + 3) % 7;
    f[7] = wd < 0 ? wd + 7 : wd;
}

static void put2(char **o, int64_t v)
{
    *(*o)++ = (char)('0' + (int)(v / 10 % 10));
    *(*o)++ = (char)('0' + (int)(v % 10));
}

/* values.fmt_time; out needs 40 bytes */
static int fmt_time(int64_t ms, char *out)
{
    int64_t f[8], ay;
    char ys[24], *o = out;
    int n, i;
    split_time(ms, f);
    ay = f[0] < 0 ? -f[0] : f[0];
    if (f[0] < 0)
        *o++ = '-';
    n = fmt_i64(ay, ys);
    for (i = n; i < 4; i++)
        *o++ = '0';
    memcpy(o, ys, (size_t)n);
    o += n;
    *o++ = '-';
    put2(&o, f[1]);
    *o++ = '-';
    put2(&o, f[2]);
    *o++ = 'T';
    put2(&o, f[3]);
    *o++ = ':';
    put2(&o, f[4]);
    *o++ = ':';
    put2(&o, f[5]);
    *o++ = '.';
    *o++ = (char)('0' + (int)(f[6] / 100));
    put2(&o, f[6] % 100);
    *o++ = 'Z';
    *o = 0;
    return (int)(o - out);
}

static int digits_at(const char *s, size_t n, size_t *pos, int k, int64_t *out)
{
    int64_t v = 0;
    int i;
    if (*pos + (size_t)k > n)
        return -1;
    for (i = 0; i < k; i++) {
        char c = s[*pos + (size_t)i];
        if (c < '0' || c > '9')
            return -1;
        v = v * 10 + (c - '0');
    }
    *pos += (size_t)k;
    *out = v;
    return 0;
}

int fs_parse_time(const char *s, size_t n, int64_t *out)
{
    size_t pos = 0;
    int64_t y, mo, d, h = 0, mi = 0, sec = 0, milli = 0, offset = 0, t, oh, om;
    if (!s || !out)
        return -1;
    if (digits_at(s, n, &pos, 4, &y) || pos >= n || s[pos++] != '-' ||
        digits_at(s, n, &pos, 2, &mo) || pos >= n || s[pos++] != '-' || digits_at(s, n, &pos, 2, &d))
        return -1;
    if (pos < n && (s[pos] == 'T' || s[pos] == ' ')) {
        pos++;
        if (digits_at(s, n, &pos, 2, &h) || pos >= n || s[pos++] != ':' || digits_at(s, n, &pos, 2, &mi))
            return -1;
        if (pos < n && s[pos] == ':') {
            pos++;
            if (digits_at(s, n, &pos, 2, &sec))
                return -1;
            if (pos < n && s[pos] == '.') {
                int k = 0;
                pos++;
                while (k < 3 && pos < n && s[pos] >= '0' && s[pos] <= '9') {
                    milli = milli * 10 + (s[pos] - '0');
                    pos++;
                    k++;
                }
                if (k == 0)
                    return -1;
                milli *= k == 1 ? 100 : k == 2 ? 10 : 1;
            }
        }
    }
    if (pos < n && s[pos] == 'Z') {
        pos++;
    } else if (pos < n && (s[pos] == '+' || s[pos] == '-')) {
        int plus = s[pos++] == '+';
        if (digits_at(s, n, &pos, 2, &oh) || pos >= n || s[pos++] != ':' || digits_at(s, n, &pos, 2, &om))
            return -1;
        if (oh > 23 || om > 59)
            return -1;
        offset = (oh * 60 + om) * 60000 * (plus ? 1 : -1);
    }
    if (pos != n)
        return -1;
    if (make_time(y, mo, d, h, mi, sec, &t))
        return -1;
    *out = t + milli - offset;
    return 0;
}

#endif /* FS_HAS_TIME */

#if FS_HAS_FLOAT
/* values.fround: half away from zero, exactly (not "fround": keep clear of C23 math names) */
static double round_half_away(double x)
{
    double a, t;
    if (x != x || x == INFINITY || x == -INFINITY)
        return x;
    a = fabs(x);
    t = floor(a);
    if (a - t >= 0.5)
        t += 1.0;
    return copysign(t, x);
}

/* values.fdiv: IEEE without relying on the compiler for x / 0 (C23 <math.h> has its own fdiv) */
static double ieee_div(double a, double b)
{
    if (b == 0.0) {
        if (a != a || a == 0.0)
            return NAN;
        return copysign(INFINITY, a) * copysign(1.0, b);
    }
    return a / b;
}
#endif /* FS_HAS_FLOAT */

/* Text of a string in traces: values.quote */
static int quote(const char *b, int n, char *out, int size)
{
    static const char hex[] = "0123456789abcdef";
    int len = 0, i;
#define QPUT(c) do { if (len < size - 1) out[len] = (c); len++; } while (0)
    QPUT('"');
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)b[i];
        if (c == '"') { QPUT('\\'); QPUT('"'); }
        else if (c == '\\') { QPUT('\\'); QPUT('\\'); }
        else if (c == '\n') { QPUT('\\'); QPUT('n'); }
        else if (c == '\t') { QPUT('\\'); QPUT('t'); }
        else if (c >= 0x20 && c < 0x7f) QPUT((char)c);
        else { QPUT('\\'); QPUT('x'); QPUT(hex[c >> 4]); QPUT(hex[c & 15]); }
    }
    QPUT('"');
#undef QPUT
    if (size > 0)
        out[len < size ? len : size - 1] = 0;
    return len;
}

/* ---- image validation -------------------------------------------------------- */
/* Is values of type t supported by this build?  (FS_NO_STR keeps str literals.) */
static int type_built(int t)
{
    return (t != FS_FLOAT || FS_HAS_FLOAT) && (t != FS_TIME || FS_HAS_TIME);
}

/* The n packed parameter types are all built.  host_str: the values come from
 * the host (event arguments), so a str would be copied into a string slot. */
static int types_built(uint32_t packed, int n, int host_str)
{
    int i;
    for (i = 0; i < n; i++)
        if (!type_built(ptype(packed, i)) || (host_str && ptype(packed, i) == FS_STR && !FS_HAS_STR))
            return 0;
    return 1;
}

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
#if FS_HAS_TIME
    fs_clock_fn clock;
#endif
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
#if FS_HAS_TIME
    clock = vm->clock;
#endif
    user = vm->user;
    memset(vm, 0, sizeof(*vm));
    vm->on_emit = on_emit;
    vm->on_fault = on_fault;
    vm->on_timer = on_timer;
#if FS_HAS_TIME
    vm->clock = clock;
#endif
    vm->user = user;
    vm->handler = -1;
    vm->state = vm->pending = vm->transition = -1;
    vm->cur_pc = -1;
    vm->cur_extern = -1;

    if (!img || len < 32)
        return img ? FS_ERR_MAGIC : FS_ERR_ARG;
    if (memcmp(img, "FSB1", 4) != 0)
        return FS_ERR_MAGIC;
    if (rd16(img + 4) != 2)
        return FS_ERR_VERSION;
    flags = rd16(img + 6);
    if (((flags & 2u) && !FS_HAS_FLOAT) || ((flags & 4u) && !FS_HAS_TIME) ||
        ((flags & 8u) && !FS_HAS_STR) || ((flags & 16u) && !FS_HAS_TEXT))
        return FS_ERR_FEATURE;
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
    if (!(vm->globals_init = take(img, len, &off, (size_t)vm->n_globals * 9)))
        return FS_ERR_TRUNCATED;
    if (!(vm->externs_tab = take(img, len, &off, (size_t)vm->n_externs * 8)))
        return FS_ERR_TRUNCATED;
    if (!(vm->events_tab = take(img, len, &off, (size_t)vm->n_events * 6)))
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

    for (i = 0; i < vm->n_globals; i++) {
        const uint8_t *g = vm->globals_init + 9 * i;
        if (g[0] > FS_TIME)
            return FS_ERR_INDEX;
        if (!type_built(g[0]))
            return FS_ERR_FEATURE;
        if (g[0] == FS_STR && rd64(g + 1) >= ns)
            return FS_ERR_INDEX;
    }
    for (i = 0; i < vm->n_externs; i++) {
        if (rd16(vm->externs_tab + 8 * i) >= ns)
            return FS_ERR_INDEX;
        if (vm->externs_tab[8 * i + 2] > 16 || vm->externs_tab[8 * i + 3] > FS_NONE_TYPE)
            return FS_ERR_LIMIT;
        if (!types_built(rd32(vm->externs_tab + 8 * i + 4), vm->externs_tab[8 * i + 2], 0) ||
            (vm->externs_tab[8 * i + 3] != FS_NONE_TYPE && !type_built(vm->externs_tab[8 * i + 3])) ||
            (vm->externs_tab[8 * i + 3] == FS_STR && !FS_HAS_STR))
            return FS_ERR_FEATURE;
    }
    for (i = 0; i < vm->n_events; i++) {
        if (rd16(vm->events_tab + 6 * i) >= ns)
            return FS_ERR_INDEX;
        if (vm->events_tab[6 * i + 2] > FS_MAX_EVENT_ARGS)
            return FS_ERR_LIMIT;
        if (!types_built(rd16(vm->events_tab + 6 * i + 4), vm->events_tab[6 * i + 2], 1))
            return FS_ERR_FEATURE;
    }
    for (i = 0; i < vm->n_states; i++)
        if (rd16(vm->states_tab + 2 * i) >= ns)
            return FS_ERR_INDEX;
    for (i = 0; i < vm->n_timers; i++)
        if (rd16(vm->timers_tab + 2 * i) >= vm->n_events)
            return FS_ERR_INDEX;
    for (i = 0; i < vm->n_events; i++)       /* built-in and timer events take no arguments */
        if (i < 3 && vm->events_tab[6 * i + 2] != 0)
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
    for (i = 0; i < vm->n_globals; i++) {
        const uint8_t *g = vm->globals_init + 9 * i;
        uint64_t u = rd64(g + 1);
        switch (g[0]) {
#if FS_HAS_FLOAT
        case FS_FLOAT: gset(vm, (int)i, fs_float(f64(u))); break;
#endif
        case FS_STR:   gset(vm, (int)i, v_str((int32_t)u)); break;
#if FS_HAS_TIME
        case FS_TIME:  gset(vm, (int)i, fs_time(s64(u))); break;
#endif
        default:       gset(vm, (int)i, fs_int(s32((uint32_t)u))); break;   /* fs_load checked the type */
        }
    }
#if FS_HAS_STR
    memset(vm->strs, 0, sizeof(vm->strs));
#endif
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
    return vm ? find_name(vm, vm->events_tab, 6, vm->n_events, name) : -1;
}

int fs_state_id(const fs_vm *vm, const char *name)
{
    return vm ? find_name(vm, vm->states_tab, 2, vm->n_states, name) : -1;
}

int fs_extern_id(const fs_vm *vm, const char *name)
{
    return vm ? find_name(vm, vm->externs_tab, 8, vm->n_externs, name) : -1;
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
    return fs_string(vm, rd16(vm->events_tab + 6 * idx), NULL);
}

const char *fs_extern_name(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return NULL;
    return fs_string(vm, rd16(vm->externs_tab + 8 * idx), NULL);
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
    return vm->events_tab[6 * idx + 2];
}

int fs_extern_argc(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return -1;
    return vm->externs_tab[8 * idx + 2];
}

int fs_event_arg_type(const fs_vm *vm, int idx, int arg)
{
    if (fs_event_argc(vm, idx) <= arg || arg < 0)
        return -1;
    return ptype(rd16(vm->events_tab + 6 * idx + 4), arg);
}

int fs_extern_arg_type(const fs_vm *vm, int idx, int arg)
{
    if (fs_extern_argc(vm, idx) <= arg || arg < 0)
        return -1;
    return ptype(rd32(vm->externs_tab + 8 * idx + 4), arg);
}

int fs_extern_ret(const fs_vm *vm, int idx)
{
    if (!vm || !vm->loaded || idx < 0 || idx >= vm->n_externs)
        return -1;
    return vm->externs_tab[8 * idx + 3];
}

int fs_current_extern(const fs_vm *vm) { return vm ? vm->cur_extern : -1; }
int fs_state(const fs_vm *vm) { return vm ? vm->state : -1; }
int fs_active(const fs_vm *vm) { return vm && vm->handler >= 0; }
int fs_handler(const fs_vm *vm) { return vm ? vm->handler : -1; }
int32_t fs_pc(const fs_vm *vm) { return vm ? vm->pc : 0; }
int fs_sp(const fs_vm *vm) { return vm ? vm->sp : 0; }
int fs_global_count(const fs_vm *vm) { return vm && vm->loaded ? vm->n_globals : 0; }
int fs_frame_count(const fs_vm *vm) { return vm ? vm->nframes : 0; }

fs_value fs_stack_at(const fs_vm *vm, int i)
{
    if (!vm || i < 0 || i >= vm->sp || i >= FS_STACK_SIZE)
        return fs_int(0);
    return cell_value(vm->stypes[i], vm->stack[i]);
}

fs_value fs_global(const fs_vm *vm, int i)
{
    if (!vm || !vm->loaded || i < 0 || i >= vm->n_globals)
        return fs_int(0);
    return gget(vm, i);
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
        if (name_eq(vm, rd16(vm->externs_tab + 8 * i), name)) {
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

/* ---- strings --------------------------------------------------------------------------- */
fs_value fs_empty_str(void) { return v_str(EMPTY_STR); }

static int str_valid(const fs_vm *vm, int32_t ref)
{
    if (ref == EMPTY_STR)
        return 1;
    if (ref >= 0)
        return ref < vm->n_strings;
#if FS_HAS_STR
    return -1 - ref < FS_STR_SLOTS && vm->strs[-1 - ref].used;
#else
    return 0;
#endif
}

const char *fs_str(const fs_vm *vm, fs_value v, uint16_t *len_out)
{
    if (!vm || !vm->loaded || v.type != FS_STR || !str_valid(vm, v.as.s))
        return NULL;
    if (v.as.s == EMPTY_STR) {
        if (len_out)
            *len_out = 0;
        return "";
    }
    if (v.as.s >= 0)
        return fs_string(vm, v.as.s, len_out);
#if FS_HAS_STR
    if (len_out)
        *len_out = vm->strs[-1 - v.as.s].len;
    return vm->strs[-1 - v.as.s].data;
#else
    return NULL;                          /* str_valid() already refused it */
#endif
}

int fs_strings_in_use(const fs_vm *vm)
{
    int n = 0;
#if FS_HAS_STR
    int i;
    for (i = 0; vm && i < FS_STR_SLOTS; i++)
        n += vm->strs[i].used;
#else
    (void)vm;
#endif
    return n;
}

#if FS_HAS_STR

static void mark(fs_vm *vm, uint8_t type, fs_cell c)
{
    if (type == FS_STR && c.s < 0 && c.s != EMPTY_STR && -1 - c.s < FS_STR_SLOTS)
        vm->strs[-1 - c.s].mark = 1;
}

/* Mark and sweep: a slot stays in use while a global, the stack or the queue refers to it. */
static void collect(fs_vm *vm)
{
    int i, j, top = vm->sp < FS_STACK_SIZE ? vm->sp : FS_STACK_SIZE;
    for (i = 0; i < FS_STR_SLOTS; i++)
        vm->strs[i].mark = 0;
    for (i = 0; i < vm->n_globals; i++)
        mark(vm, vm->gtypes[i], vm->globals[i]);
    for (i = 0; i < top; i++)
        mark(vm, vm->stypes[i], vm->stack[i]);
    for (i = 0; i < vm->q_count; i++) {
        const fs_qentry *e = &vm->queue[(vm->q_head + i) % FS_QUEUE_SIZE];
        for (j = 0; j < FS_MAX_EVENT_ARGS; j++)
            mark(vm, e->types[j], e->args[j]);
    }
    for (i = 0; i < FS_STR_SLOTS; i++)
        if (!vm->strs[i].mark)
            vm->strs[i].used = 0;
}

/* A new string of n bytes copied from p (may be NULL: filled by the caller).
 * Returns 0 and the value, or the fault code (12 too long, 11 no slot). */
static int alloc_str(fs_vm *vm, const char *p, size_t n, fs_value *out)
{
    int i, attempt;
    if (n > FS_STR_LEN)
        return FS_FAULT_STR_LONG;
    for (attempt = 0; attempt < 2; attempt++) {
        for (i = 0; i < FS_STR_SLOTS; i++) {
            if (!vm->strs[i].used) {
                fs_strslot *sl = &vm->strs[i];
                sl->used = 1;
                sl->len = (uint16_t)n;
                if (p && n)
                    memcpy(sl->data, p, n);
                sl->data[n] = 0;
                *out = v_str(-1 - i);
                return 0;
            }
        }
        if (attempt == 0)
            collect(vm);
    }
    return FS_FAULT_NO_STRINGS;
}
#endif /* FS_HAS_STR */

/* A value from the host as type t (vm.py host_value).  Returns 0 or a fault code. */
static int host_value(fs_vm *vm, fs_value v, int t, fs_value *out)
{
#if FS_HAS_FLOAT
    if (t == FS_FLOAT && v.type == FS_INT) {
        *out = fs_float((double)v.as.i);
        return 0;
    }
#endif
#if FS_HAS_STR
    if (t == FS_STR && v.type == FS_CSTR)
        return alloc_str(vm, v.as.c, strlen(v.as.c), out);
#endif
    if (v.type != t)
        return FS_FAULT_TYPE;
    if (t == FS_STR && !str_valid(vm, v.as.s))
        return FS_FAULT_TYPE;
    *out = v;
    return 0;
}

int fs_format(const fs_vm *vm, fs_value v, char *buf, int size)
{
    char tmp[48];
    int n;
    if (!buf || size <= 0)
        return 0;
    switch (v.type) {
    case FS_INT:   n = fmt_i64(v.as.i, tmp); break;
#if FS_HAS_FLOAT
    case FS_FLOAT: n = fmt_float(v.as.f, tmp); break;
#endif
#if FS_HAS_TIME
    case FS_TIME:  n = fmt_time(v.as.t, tmp); break;
#endif
    case FS_STR: {
        uint16_t len = 0;
        const char *p = fs_str(vm, v, &len);
        return quote(p ? p : "", p ? len : 0, buf, size);
    }
#if FS_HAS_STR
    case FS_CSTR:
        return quote(v.as.c, (int)strlen(v.as.c), buf, size);
#endif
    default:
        n = 0;
        tmp[0] = 0;
    }
    memcpy(buf, tmp, (size_t)(n < size ? n + 1 : size));
    buf[size - 1 < n ? size - 1 : n] = 0;
    return n;
}

void fs_start(fs_vm *vm)
{
    if (!vm || !vm->loaded)
        return;
    vm->boot_pending = 1;
    if (vm->n_states)
        vm->pending = 0;
}

int fs_post(fs_vm *vm, int event, const fs_value *args, int argc)
{
    fs_qentry *e;
    int i, n, pc;
    uint16_t types;
    if (!vm || !vm->loaded || event < 0 || event >= vm->n_events)
        return FS_ERR_ARG;
    pc = vm->handler >= 0 ? (int)vm->cur_pc : -1;
    if (vm->q_count >= FS_QUEUE_SIZE) {
        report_fault(vm, FS_FAULT_QUEUE_FULL, pc);
        return FS_ERR_FULL;
    }
    /* the entry is queued first, so the strings already copied are roots */
    e = &vm->queue[(vm->q_head + vm->q_count) % FS_QUEUE_SIZE];
    vm->q_count++;
    e->event = (uint16_t)event;
    n = vm->events_tab[6 * event + 2];          /* <= FS_MAX_EVENT_ARGS */
    types = rd16(vm->events_tab + 6 * event + 4);
    for (i = 0; i < FS_MAX_EVENT_ARGS; i++)
        qset(e, i, fs_int(0));
    for (i = 0; i < n; i++) {
        int t = ptype(types, i), err;
        fs_value v = (args && i < argc) ? args[i] : zero_of(t), stored;
        err = host_value(vm, v, t, &stored);
        if (!err)
            qset(e, i, stored);
        if (err) {
            vm->q_count--;
            if (err == FS_FAULT_TYPE)
                return FS_ERR_ARG;
            report_fault(vm, err, pc);
            return FS_ERR_MEM;
        }
    }
    return FS_OK;
}

void fs_set_clock(fs_vm *vm, fs_clock_fn fn)
{
#if FS_HAS_TIME
    if (vm)
        vm->clock = fn;
#else
    (void)vm;
    (void)fn;
#endif
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
    return fs_string(vm, rd16(vm->events_tab + 6 * rd16(vm->timers_tab + 2 * timer)), NULL);
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

/* e: the queued event whose arguments start the handler, or NULL */
static void begin(fs_vm *vm, int h, const fs_qentry *e)
{
    const uint8_t *p = vm->handlers_tab + 8 * h;
    int argc = p[4], nlocals = p[5], n, i;
    n = argc > nlocals ? argc : nlocals;
    for (i = 0; i < n && i < FS_STACK_SIZE; i++)
        sset(vm, i, (e && i < argc && i < FS_MAX_EVENT_ARGS) ? qget(e, i) : fs_int(0));
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
            begin(vm, h, e);
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
/* Stack slot access; slots >= FS_STACK_SIZE are virtual int zeros (see fs_vm.sp). */
#define SLOT(i) ((i) < FS_STACK_SIZE ? cell_value(vm->stypes[(i)], vm->stack[(i)]) : fs_int(0))

#define FAULT(c) do { fault = (c); goto faulted; } while (0)
#define POP(v) do { if (vm->sp <= 0) FAULT(FS_FAULT_STACK_UNDERFLOW); \
                    vm->sp--; (v) = SLOT(vm->sp); } while (0)
#define PUSH(v) do { fs_value pv_ = (v); if (vm->sp >= FS_STACK_SIZE) FAULT(FS_FAULT_STACK_OVERFLOW); \
                     sset(vm, vm->sp++, pv_); } while (0)
#define POPINT(v) do { fs_value pi_; POP(pi_); if (pi_.type != FS_INT) FAULT(FS_FAULT_TYPE); (v) = pi_.as.i; } while (0)

/* externs take at most 16 arguments (fs_load), built-ins 6, events FS_MAX_EVENT_ARGS */
#define MAX_CALL_ARGS (FS_MAX_EVENT_ARGS > 16 ? FS_MAX_EVENT_ARGS : 16)

/* The top n values (caller checked sp >= n), left on the stack, gathered into
 * tmp (MAX_CALL_ARGS entries) as fs_values for type checks and callbacks.
 * Returns NULL if n does not fit, which only hand-crafted bytecode can cause. */
static const fs_value *arg_view(fs_vm *vm, int n, fs_value *tmp)
{
    int base = vm->sp - n, i;
    if (n > MAX_CALL_ARGS)
        return NULL;
    for (i = 0; i < n; i++)
        tmp[i] = SLOT(base + i);
    return tmp;
}

static int check_types(const fs_value *args, int n, uint32_t types)
{
    int i;
    for (i = 0; i < n; i++)
        if (args[i].type != ptype(types, i))
            return FS_FAULT_TYPE;
    return 0;
}

static const char *sbytes(const fs_vm *vm, fs_value v, int *len)
{
    uint16_t n = 0;
    const char *p = fs_str(vm, v, &n);
    *len = n;
    return p ? p : "";
}

/* values.binary for opcode op on a and b (still on the stack). */
static int binop(fs_vm *vm, uint8_t op, fs_value a, fs_value b, fs_value *r)
{
    int cmp = op >= OP_EQ && op <= OP_GE;
    if (op == OP_CONCAT && !FS_HAS_STR)
        return FS_FAULT_BAD_OPCODE;      /* left out of this build (FS_NO_STR) */
    if (op == OP_CONCAT && !(a.type == FS_STR && b.type == FS_STR))
        return FS_FAULT_TYPE;            /* strings are joined by CONCAT, numbers by ADD */
    if (a.type == FS_INT && b.type == FS_INT) {
        int32_t x = a.as.i, y = b.as.i, v;
        uint32_t ux = (uint32_t)x, uy = (uint32_t)y;
        switch (op) {
        case OP_ADD: v = s32(ux + uy); break;
        case OP_SUB: v = s32(ux - uy); break;
        case OP_MUL: v = s32(ux * uy); break;
        case OP_DIV:
            if (y == 0)
                return FS_FAULT_DIV_ZERO;
            v = (x == INT32_MIN && y == -1) ? INT32_MIN : x / y;
            break;
        case OP_MOD:
            if (y == 0)
                return FS_FAULT_DIV_ZERO;
            v = (y == -1) ? 0 : x % y;
            break;
        case OP_BAND: v = s32(ux & uy); break;
        case OP_BOR:  v = s32(ux | uy); break;
        case OP_BXOR: v = s32(ux ^ uy); break;
        case OP_SHL:  v = s32(ux << (uy & 31u)); break;
        case OP_SHR: {
            unsigned n = uy & 31u;
            /* arithmetic shift without relying on implementation-defined >> */
            v = x >= 0 ? (int32_t)((uint32_t)x >> n) : -(int32_t)((uint32_t)(-(x + 1)) >> n) - 1;
            break;
        }
        case OP_EQ: v = x == y; break;
        case OP_NE: v = x != y; break;
        case OP_LT: v = x < y; break;
        case OP_LE: v = x <= y; break;
        case OP_GT: v = x > y; break;
        default:    v = x >= y; break;
        }
        *r = fs_int(v);
        return 0;
    }
#if FS_HAS_FLOAT
    if (a.type == FS_FLOAT && b.type == FS_FLOAT) {
        double x = a.as.f, y = b.as.f;
        switch (op) {
        case OP_ADD: *r = fs_float(x + y); return 0;
        case OP_SUB: *r = fs_float(x - y); return 0;
        case OP_MUL: *r = fs_float(x * y); return 0;
        case OP_DIV: *r = fs_float(ieee_div(x, y)); return 0;
        case OP_MOD: *r = fs_float(fmod(x, y)); return 0;
        case OP_EQ: *r = fs_int(x == y); return 0;
        case OP_NE: *r = fs_int(x != y); return 0;
        case OP_LT: *r = fs_int(x < y); return 0;
        case OP_LE: *r = fs_int(x <= y); return 0;
        case OP_GT: *r = fs_int(x > y); return 0;
        case OP_GE: *r = fs_int(x >= y); return 0;
        default: return FS_FAULT_TYPE;
        }
    }
#endif
    if (a.type == FS_STR && b.type == FS_STR && (op == OP_CONCAT || cmp)) {
        int la, lb, c;
        const char *pa = sbytes(vm, a, &la), *pb = sbytes(vm, b, &lb);
#if FS_HAS_STR
        if (op == OP_CONCAT) {
            int err;
            fs_value s;
            if (la + lb > FS_STR_LEN)
                return FS_FAULT_STR_LONG;
            if ((err = alloc_str(vm, NULL, (size_t)(la + lb), &s)) != 0)
                return err;
            /* pa/pb stay valid: a and b are on the stack, slots never move */
            memcpy(vm->strs[-1 - s.as.s].data, pa, (size_t)la);
            memcpy(vm->strs[-1 - s.as.s].data + la, pb, (size_t)lb);
            *r = s;
            return 0;
        }
#endif
        c = memcmp(pa, pb, (size_t)(la < lb ? la : lb));
        if (c == 0)
            c = la - lb;
        switch (op) {
        case OP_EQ: *r = fs_int(c == 0); break;
        case OP_NE: *r = fs_int(c != 0); break;
        case OP_LT: *r = fs_int(c < 0); break;
        case OP_LE: *r = fs_int(c <= 0); break;
        case OP_GT: *r = fs_int(c > 0); break;
        default:    *r = fs_int(c >= 0); break;
        }
        return 0;
    }
#if FS_HAS_TIME
    if (a.type == FS_TIME && b.type == FS_INT && (op == OP_ADD || op == OP_SUB)) {
        uint64_t t = (uint64_t)a.as.t, d = (uint64_t)(int64_t)b.as.i;
        *r = fs_time(s64(op == OP_ADD ? t + d : t - d));
        return 0;
    }
    if (a.type == FS_INT && b.type == FS_TIME && op == OP_ADD) {
        *r = fs_time(s64((uint64_t)b.as.t + (uint64_t)(int64_t)a.as.i));
        return 0;
    }
    if (a.type == FS_TIME && b.type == FS_TIME && (op == OP_SUB || cmp)) {
        int64_t x = a.as.t, y = b.as.t;
        if (op == OP_SUB) {
            int64_t d = s64((uint64_t)x - (uint64_t)y);
            *r = fs_int(d > INT32_MAX ? INT32_MAX : d < INT32_MIN ? INT32_MIN : (int32_t)d);
            return 0;
        }
        switch (op) {
        case OP_EQ: *r = fs_int(x == y); break;
        case OP_NE: *r = fs_int(x != y); break;
        case OP_LT: *r = fs_int(x < y); break;
        case OP_LE: *r = fs_int(x <= y); break;
        case OP_GT: *r = fs_int(x > y); break;
        default:    *r = fs_int(x >= y); break;
        }
        return 0;
    }
#endif
    return FS_FAULT_TYPE;
}

/* values.BUILTINS: parameter types by id (I int, F float, S str, T time) */
static const char *const BUILTIN_PARAMS[] = {
    "I", "S", "T", "F", "S", "I", "F", "T", "S", "III", "IIIIII", "", "T",
    "T", "T", "T", "T", "T", "T", "T", "T", "S", "SII", "SS", "I", "F",
    "II", "II", "FF", "FF", "F", "F", "F", "F", "F", "F", "FF"
};
#define N_BUILTINS ((unsigned)(sizeof(BUILTIN_PARAMS) / sizeof(BUILTIN_PARAMS[0])))

static uint32_t builtin_types(unsigned id, int *argc)
{
    const char *p = BUILTIN_PARAMS[id];
    uint32_t t = 0;
    int i;
    for (i = 0; p[i]; i++)
        t |= (uint32_t)(p[i] == 'I' ? FS_INT : p[i] == 'F' ? FS_FLOAT : p[i] == 'S' ? FS_STR : FS_TIME) << (2 * i);
    *argc = i;
    return t;
}

#if FS_HAS_STR
static int new_text(fs_vm *vm, const char *p, int n, fs_value *r)
{
    return alloc_str(vm, p, (size_t)n, r);
}
#endif

/* values.call_builtin; a = arguments (still on the stack). */
/* Built-ins that need a left-out feature are not compiled in: they fall to
 * `default` and fault like an unknown id (fs_load already refuses images
 * whose feature flags ask for them). */
static int builtin(fs_vm *vm, unsigned id, const fs_value *a, fs_value *r)
{
#if FS_HAS_STR
    char tmp[48];
#endif
    int n;
    switch (id) {
#if FS_HAS_FLOAT
    case 0: *r = fs_float((double)a[0].as.i); return 0;
#if FS_HAS_TEXT
    case 1: {
        double f;
        const char *p = sbytes(vm, a[0], &n);
        if (fs_parse_float(p, (size_t)n, &f))
            return FS_FAULT_VALUE;
        *r = fs_float(f);
        return 0;
    }
#endif
#if FS_HAS_TIME
    case 2: *r = fs_float((double)a[0].as.t / 1000.0); return 0;
#endif
    case 3: {
        double x = a[0].as.f;
        if (x != x || x >= 2147483648.0 || x <= -2147483649.0)
            return FS_FAULT_VALUE;
        *r = fs_int((int32_t)x);
        return 0;
    }
#endif /* FS_HAS_FLOAT */
    case 4: {
        int32_t v;
        const char *p = sbytes(vm, a[0], &n);
        if (parse_i32(p, (size_t)n, &v))
            return FS_FAULT_VALUE;
        *r = fs_int(v);
        return 0;
    }
#if FS_HAS_STR
    case 5: n = fmt_i64(a[0].as.i, tmp); return new_text(vm, tmp, n, r);
#if FS_HAS_FLOAT && FS_HAS_TEXT
    case 6: n = fmt_float(a[0].as.f, tmp); return new_text(vm, tmp, n, r);
#endif
#if FS_HAS_TIME && FS_HAS_TEXT
    case 7: n = fmt_time(a[0].as.t, tmp); return new_text(vm, tmp, n, r);
#endif
#endif /* FS_HAS_STR */
#if FS_HAS_TIME
#if FS_HAS_TEXT
    case 8: {
        int64_t t;
        const char *p = sbytes(vm, a[0], &n);
        if (fs_parse_time(p, (size_t)n, &t))
            return FS_FAULT_VALUE;
        *r = fs_time(t);
        return 0;
    }
#endif
    case 9: case 10: {
        int64_t t;
        if (make_time(a[0].as.i, a[1].as.i, a[2].as.i, id == 10 ? a[3].as.i : 0,
                      id == 10 ? a[4].as.i : 0, id == 10 ? a[5].as.i : 0, &t))
            return FS_FAULT_VALUE;
        *r = fs_time(t);
        return 0;
    }
    case 11: *r = fs_time(vm->clock ? vm->clock(vm, vm->user) : 0); return 0;
    case 12: *r = fs_time(floor_div(a[0].as.t, DAY_MS) * DAY_MS); return 0;
    case 13: case 14: case 15: case 16: case 17: case 18: case 19: case 20: {
        int64_t f[8];
        split_time(a[0].as.t, f);
        *r = fs_int(s32((uint32_t)(uint64_t)f[id - 13]));
        return 0;
    }
#endif /* FS_HAS_TIME */
    case 21: sbytes(vm, a[0], &n); *r = fs_int(n); return 0;
#if FS_HAS_STR
    case 22: {
        int32_t start = a[1].as.i, count = a[2].as.i, end;
        const char *p = sbytes(vm, a[0], &n);
        int err;
        if (start < 0)
            start = 0;
        if (start > n)
            start = n;
        if (count < 0)
            count = 0;
        end = count < n - start ? start + count : n;
        if ((err = alloc_str(vm, NULL, (size_t)(end - start), r)) != 0)
            return err;
        memcpy(vm->strs[-1 - r->as.s].data, p + start, (size_t)(end - start));
        return 0;
    }
#endif
    case 23: {
        int lb, i, found = -1;
        const char *p = sbytes(vm, a[0], &n), *q = sbytes(vm, a[1], &lb);
        for (i = 0; i + lb <= n; i++) {
            if (memcmp(p + i, q, (size_t)lb) == 0) {
                found = i;
                break;
            }
        }
        *r = fs_int(found);
        return 0;
    }
    case 24: *r = fs_int(s32(a[0].as.i < 0 ? 0u - (uint32_t)a[0].as.i : (uint32_t)a[0].as.i)); return 0;
#if FS_HAS_FLOAT
    case 25: *r = fs_float(fabs(a[0].as.f)); return 0;
#endif
    case 26: *r = fs_int(a[1].as.i < a[0].as.i ? a[1].as.i : a[0].as.i); return 0;
    case 27: *r = fs_int(a[1].as.i > a[0].as.i ? a[1].as.i : a[0].as.i); return 0;
#if FS_HAS_FLOAT
    case 28: *r = fs_float(a[1].as.f < a[0].as.f ? a[1].as.f : a[0].as.f); return 0;
    case 29: *r = fs_float(a[1].as.f > a[0].as.f ? a[1].as.f : a[0].as.f); return 0;
    case 30: *r = fs_float(floor(a[0].as.f)); return 0;
    case 31: *r = fs_float(ceil(a[0].as.f)); return 0;
    case 32: *r = fs_float(round_half_away(a[0].as.f)); return 0;
    case 33: *r = fs_float(sqrt(a[0].as.f)); return 0;
    case 34: *r = fs_float(exp(a[0].as.f)); return 0;
    case 35: *r = fs_float(log(a[0].as.f)); return 0;
    case 36: *r = fs_float(pow(a[0].as.f, a[1].as.f)); return 0;
#endif
    default: return FS_FAULT_BAD_OPCODE;
    }
}

static int execute(fs_vm *vm)
{
    const uint8_t *code = vm->code;
    uint32_t csize = vm->code_size;
    int32_t pc = vm->pc, npc;
    unsigned arg = 0;
    fs_value v, r;
    int32_t iv;
    int fault, err;
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
    case OP_NOT: case OP_BOOL: case OP_RET: case OP_RET0: case OP_CONCAT:
        npc = pc + 1;
        break;
    case OP_PUSH8: case OP_LOADL: case OP_STOREL: case OP_BUILTIN:
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
    case OP_PUSHF: case OP_PUSHT:
        if ((uint32_t)pc + 9 > csize)
            FAULT(FS_FAULT_BAD_OPCODE);
        arg = 0;
        npc = pc + 9;
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
        PUSH(fs_int(s32(rd32(code + pc + 1))));
        break;
    case OP_PUSH8:
        PUSH(fs_int(arg < 0x80 ? (int32_t)arg : (int32_t)arg - 256));
        break;
    case OP_PUSHF:
#if FS_HAS_FLOAT
        PUSH(fs_float(f64(rd64(code + pc + 1))));
        break;
#else
        FAULT(FS_FAULT_BAD_OPCODE);
#endif
    case OP_PUSHT:
#if FS_HAS_TIME
        PUSH(fs_time(s64(rd64(code + pc + 1))));
        break;
#else
        FAULT(FS_FAULT_BAD_OPCODE);
#endif
    case OP_PUSHS:
        if (arg >= vm->n_strings)
            FAULT(FS_FAULT_BAD_OPCODE);
        PUSH(v_str((int32_t)arg));
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
        PUSH(gget(vm, (int)arg));
        break;
    case OP_STOREG:
        if (arg >= vm->n_globals)
            FAULT(FS_FAULT_BAD_OPCODE);
        POP(v);
        gset(vm, (int)arg, v);
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
            sset(vm, i, v);
        break;
    }
    case OP_ADD: case OP_SUB: case OP_MUL: case OP_DIV: case OP_MOD:
    case OP_BAND: case OP_BOR: case OP_BXOR: case OP_SHL: case OP_SHR:
    case OP_EQ: case OP_NE: case OP_LT: case OP_LE: case OP_GT: case OP_GE: case OP_CONCAT:
        if (vm->sp < 2)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        /* the operands stay on the stack (string roots) until the result exists */
        if ((err = binop(vm, op, SLOT(vm->sp - 2), SLOT(vm->sp - 1), &r)) != 0)
            FAULT(err);
        vm->sp -= 2;
        PUSH(r);
        break;
    case OP_NEG:
        POP(v);
        if (v.type == FS_INT)
            PUSH(fs_int(s32(0u - (uint32_t)v.as.i)));
#if FS_HAS_FLOAT
        else if (v.type == FS_FLOAT)
            PUSH(fs_float(-v.as.f));
#endif
        else
            FAULT(FS_FAULT_TYPE);
        break;
    case OP_BNOT:
        POPINT(iv);
        PUSH(fs_int(s32(~(uint32_t)iv)));
        break;
    case OP_NOT:
        POPINT(iv);
        PUSH(fs_int(iv == 0));
        break;
    case OP_BOOL:
        POPINT(iv);
        PUSH(fs_int(iv != 0));
        break;
    case OP_JMP:
        vm->pc = (int32_t)arg;   /* an out-of-range target faults on the next step */
        break;
    case OP_JZ:
        POPINT(iv);
        if (iv == 0)
            vm->pc = (int32_t)arg;
        break;
    case OP_JNZ:
        POPINT(iv);
        if (iv != 0)
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
            PUSH(fs_int(0));
        vm->frames[vm->nframes].ret_pc = npc;
        vm->frames[vm->nframes].base = base;
        vm->nframes++;
        vm->pc = rd16(f + 4);
        break;
    }
    case OP_RET:
    case OP_RET0: {
        const fs_frame *fr;
        v = fs_int(0);
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
        fs_value tmp[MAX_CALL_ARGS];
        const fs_value *args;
        const uint8_t *x;
        int xargc, ret;
        fs_extern_fn fn;
        if (arg >= vm->n_externs)
            FAULT(FS_FAULT_BAD_OPCODE);
        x = vm->externs_tab + 8 * arg;
        xargc = x[2];
        ret = x[3];
        if (vm->sp < xargc)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        args = arg_view(vm, xargc, tmp);
        if (!args)                   /* only reachable with virtual slots */
            FAULT(FS_FAULT_STACK_OVERFLOW);
        if ((err = check_types(args, xargc, rd32(x + 4))) != 0)
            FAULT(err);
        fn = vm->bound[arg];
        if (!fn)
            FAULT(FS_FAULT_UNBOUND);
        vm->cur_extern = (int)arg;
        v = fn(vm, args, xargc, vm->user);   /* the arguments stay on the stack meanwhile */
        vm->cur_extern = -1;
        if (ret == FS_NONE_TYPE)
            r = fs_int(0);
        else if ((err = host_value(vm, v, ret, &r)) != 0)
            FAULT(err);
        vm->sp -= xargc;
        PUSH(r);
        break;
    }
    case OP_BUILTIN: {
        fs_value tmp[MAX_CALL_ARGS];
        const fs_value *args;
        int bargc;
        uint32_t types;
        if (arg >= N_BUILTINS)
            FAULT(FS_FAULT_BAD_OPCODE);
        types = builtin_types(arg, &bargc);
        if (vm->sp < bargc)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        args = arg_view(vm, bargc, tmp);
        if (!args)
            FAULT(FS_FAULT_STACK_OVERFLOW);
        if ((err = check_types(args, bargc, types)) != 0)
            FAULT(err);
        if ((err = builtin(vm, arg, args, &r)) != 0)
            FAULT(err);
        vm->sp -= bargc;
        PUSH(r);
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
        fs_value tmp[MAX_CALL_ARGS], copy[FS_MAX_EVENT_ARGS];
        const fs_value *args;
        int eargc, i;
        if (arg >= vm->n_events)
            FAULT(FS_FAULT_BAD_OPCODE);
        eargc = vm->events_tab[6 * arg + 2];      /* <= FS_MAX_EVENT_ARGS */
        if (vm->sp < eargc)
            FAULT(FS_FAULT_STACK_UNDERFLOW);
        args = arg_view(vm, eargc, tmp);
        if (!args)
            FAULT(FS_FAULT_STACK_OVERFLOW);
        if ((err = check_types(args, eargc, rd16(vm->events_tab + 6 * arg + 4))) != 0)
            FAULT(err);
        for (i = 0; i < eargc; i++)
            copy[i] = args[i];    /* the callback may not see stack slots change */
        if (vm->on_emit)
            vm->on_emit(vm, (int)arg, copy, eargc, vm->user);
        /* queue-full is reported but does not abort the handler (as vm.py) */
        fs_post(vm, (int)arg, copy, eargc);
        vm->sp -= eargc;
        break;
    }
    case OP_TSTART:
    case OP_TEVERY: {
        fs_timer *t;
        if (arg >= vm->n_timers)
            FAULT(FS_FAULT_BAD_OPCODE);
        POPINT(iv);
        t = &vm->timers[arg];
        t->active = 1;
        t->periodic = op == OP_TEVERY;
        t->period = iv;
        t->remaining = iv;
        t->owner = (int16_t)handler_state(vm, vm->handler);
        if (vm->on_timer)
            vm->on_timer(vm, (int)arg, 1, iv, t->periodic, vm->user);
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
