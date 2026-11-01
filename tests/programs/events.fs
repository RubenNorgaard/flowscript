# Event arguments, handler lookup, raise_event + queue overflow
flow events

extern show(a, b, c)

event three(a, b, c)
event few(a, b)
event noargs
event flood(n)
event item(i)
event unhandled(x)

var items = 0

any_state:
  # global handler, overridden in state "two"
  on three(a, b, c):
    show(a, b, c)

  # handler names fewer parameters than the event has
  on few(x):
    show(x, 0, -1)

  on noargs:
    show(1, 2, 3)
    raise_event unhandled(5)
    raise_event three(7, 8, 9)

  on flood(n):
    var i = 0
    while i < n:
      raise_event item(i)
      i = i + 1
    show(i, items, 0)

  on item(i):
    items = items + 1

initial state one:
  on noargs:
    goto_state two

state two:
  on three(p, q, r):
    show(r, q, p)

  on few(a, b):
    show(b, a, 99)
    goto_state one
