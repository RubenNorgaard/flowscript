# Program used by c/test_api.c (not part of the trace conformance run)
flow api version 2

extern record(a, b)
extern flood()

event noop
event rec(a, b)
event doflood

timer t

var g = 5

any_state:
  on boot:
    start t 10

  on rec(a, b):
    record(a, b)

  on doflood:
    flood()
