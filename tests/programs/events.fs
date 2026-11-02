# Event arguments, handler lookup, raise_event + queue overflow
flow events

extern show(int a, int b, int c) -> int

event three(int a, int b, int c)
event few(int a, int b)
event noargs
event flood(int n)
event item(int i)
event unhandled(int x)

int items = 0

initial state one:
  on noargs:
    goto_state two

  # (each state handles the events it reacts to; "two" has its own version)
  on three(a, b, c):
    show(a, b, c)

  # handler names fewer parameters than the event has
  on few(x):
    show(x, 0, -1)

  on flood(n):
    int i = 0
    while i < n:
      raise_event item(i)
      i = i + 1
    show(i, items, 0)

  on item(i):
    items = items + 1

state two:
  on three(p, q, r):
    show(r, q, p)

  on few(a, b):
    show(b, a, 99)
    goto_state one

  on noargs:
    show(1, 2, 3)
    raise_event unhandled(5)
    raise_event three(7, 8, 9)

  on flood(n):
    int i = 0
    while i < n:
      raise_event item(i)
      i = i + 1
    show(i, items, 0)

  on item(i):
    items = items + 1
