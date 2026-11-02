# Program used by c/test_api.c (not part of the trace conformance run)
flow api version 2

extern record(int a, int b) -> int
extern flood() -> int
extern echo(str s) -> str
extern wrong() -> float

event noop
event rec(int a, int b)
event doflood
event text(str s, float x)
event mistype

timer t

int g = 5
str last = "none"
float lastx = 0

state main:
  on boot:
    start t 10

  on rec(a, b):
    record(a, b)

  on doflood:
    flood()

  on text(s, x):
    last = echo(s + "!")
    lastx = x

  on mistype:
    lastx = wrong()
