# int32 wrap-around, truncating division, masked shifts
flow arith version 1

event calc(a, b)
event divmod(a, b)
event shifts(a, b)
event unary(a)
event out(r1, r2, r3, r4, r5, r6, r7, r8)

# constant folding uses the same int32 semantics
const MAXI = 2147483647

var wrap_add = MAXI + 1
var min_div = -2147483648 / -1
var min_mod = -2147483648 % -1
var neg_mod = -7 % 3
var fold_shr = -8 >> 1
var fold_shl = 1 << 33
var fold_mul = 65536 * 65536
var hex = 0xFFFFFFFF
var last = 0

any_state:
  on calc(a, b):
    raise_event out(a + b, a - b, a * b, a & b, a | b, a ^ b, a == b, a < b)
    raise_event out(a <= b, a > b, a >= b, a != b, -a, ~b, a + 1, b - 1)

  on divmod(a, b):
    last = 1
    var q = a / b
    last = 2
    var r = a % b
    last = 3
    raise_event out(q, r, q * b + r, 0, 0, 0, 0, 0)

  on shifts(a, b):
    raise_event out(a << b, a >> b, a << 31, a >> 31, a << 32, a >> 32, a << -1, a >> -1)

  on unary(a):
    raise_event out(-a, ~a, not a, a and 1, a or 0, a xor 1, --a, ~~a)
