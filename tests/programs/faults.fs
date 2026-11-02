# Faults: abort the handler, report pc, continue with the next event
flow faults

extern ping(int x) -> int
extern wide(int p0, int p1, int p2, int p3, int p4, int p5, int p6, int p7, int p8, int p9, int p10, int p11, int p12, int p13, int p14, int p15) -> int

event recurse(int n)
event depth(int n)
event spin
event almost
event deep
event many
event after
event widecall

int counter = 0
int v = 1

func rec(int n) -> int:
  return rec(n + 1)

func down(int n) -> int:
  if n > 0:
    return down(n - 1) + 1
  return 0

func lots() -> int:
  int l00 = 0
  int l01 = 0
  int l02 = 0
  int l03 = 0
  int l04 = 0
  int l05 = 0
  int l06 = 0
  int l07 = 0
  int l08 = 0
  int l09 = 0
  int l10 = 0
  int l11 = 0
  int l12 = 0
  int l13 = 0
  int l14 = 0
  int l15 = 0
  int l16 = 0
  int l17 = 0
  int l18 = 0
  int l19 = 0
  int m00 = 0
  int m01 = 0
  int m02 = 0
  int m03 = 0
  int m04 = 0
  int m05 = 0
  int m06 = 0
  int m07 = 0
  int m08 = 0
  int m09 = 0
  return 7

func lots_rec(int n) -> int:
  int a = n
  int b = n
  int c = n
  if n > 0:
    return lots_rec(n - 1)
  return lots()

state main:
  on recurse(n):
    counter = 100
    ping(rec(n))
    counter = 101

  on depth(n):
    ping(down(n))

  on spin:
    counter = 0
    while 1:
      counter = counter + 1

  # a loop that just fits into the budget of 10000 instructions
  on almost:
    int i = 0
    while i < 1110:
      i = i + 1
    ping(i)

  on deep:
    counter = v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + 1)))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))

  on many:
    ping(lots())
    ping(lots_rec(2))
    ping(lots_rec(3))
    ping(lots_rec(4))
    ping(lots_rec(5))
    ping(lots_rec(6))
    ping(lots_rec(1))

  on widecall:
    counter = 5
    # 15 args pending per level -> > 64 stack slots
    wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, v)))))

  on after:
    ping(counter)
