# typed events, externs that return values, implicit int -> float, faults of the new types
flow types version 1

extern ratio(int a, float b) -> float
extern label(int n) -> str
extern stamp() -> time
extern note(str what, float x, time t, int n)

event mixed(int n, float x, str s, time t)
event echo(str s, float x)
event relabel(int n)

float f = 1
str s

func half(float x) -> float:
  return x / 2

func describe(int n) -> str:
  if n > 0:
    return "pos " + str(n)
  return "non-pos"

any_state:
  on boot:
    f = half(3)
    note(describe(2) + describe(0), f, stamp(), 7)
    float r = ratio(2, 3)
    note(label(5), r * 2, stamp() + 10, int(r))

  on mixed(n, x, s, t):
    note(s, x + n, t, n)
    raise_event echo(s + "!", n)

  on echo(s, x):
    note(s, x, stamp(), 0)

  on relabel(n):
    note(label(n), 0, stamp(), n)
