# Short-circuit and/or, xor/not with stubbed externs
flow logic

extern a() -> int
extern b() -> int
extern c() -> int
extern mark(int x) -> int

event test

int hits = 0

state main:
  on test:
    if a() and b():
      mark(1)
    if a() or b():
      mark(2)
    if not a() and not b() or c():
      mark(3)
    if a() xor b():
      mark(4)
    if not (a() xor c()):
      mark(5)
    int v = a() and b() or c()
    mark(v)
    v = (a() or b()) and c()
    mark(v)
    v = a() xor b() xor c()
    mark(v)
    v = not a()
    mark(v)
    while a() and hits < 3:
      hits = hits + 1
    mark(hits)
    if a() > 1 and b() <= 5 or c() == -1:
      mark(6)
