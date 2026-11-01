# int32 wrap-around, truncating division, masked shifts
flow arith version 1

event calc(int a, int b)
event divmod(int a, int b)
event shifts(int a, int b)
event unary(int a)
event out(int r1, int r2, int r3, int r4, int r5, int r6, int r7, int r8)

# constant folding uses the same int32 semantics
const MAXI = 2147483647

int wrap_add = MAXI + 1
int min_div = -2147483648 / -1
int min_mod = -2147483648 % -1
int neg_mod = -7 % 3
int fold_shr = -8 >> 1
int fold_shl = 1 << 33
int fold_mul = 65536 * 65536
int hex = 0xFFFFFFFF
int last = 0

any_state:
  on calc(a, b):
    raise_event out(a + b, a - b, a * b, a & b, a | b, a ^ b, a == b, a < b)
    raise_event out(a <= b, a > b, a >= b, a != b, -a, ~b, a + 1, b - 1)

  on divmod(a, b):
    last = 1
    int q = a / b
    last = 2
    int r = a % b
    last = 3
    raise_event out(q, r, q * b + r, 0, 0, 0, 0, 0)

  on shifts(a, b):
    raise_event out(a << b, a >> b, a << 31, a >> 31, a << 32, a >> 32, a << -1, a >> -1)

  on unary(a):
    raise_event out(-a, ~a, not a, a and 1, a or 0, a xor 1, --a, ~~a)
