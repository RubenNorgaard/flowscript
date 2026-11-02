# Functions with several results: a, b, c = f(...) assigns them in order.

extern log(str what, float value)

event go(int n)

const SCALE = 2.5

float total = 0
int count = 0
str label = ""

# Three results of different types; an int result may go into a float variable
func split(int n) -> int, float, str:
  if n < 0:
    return 0, 0.0, "negative"
  return n * 2, n * SCALE, "n=" + str(n)

# Falling off the end returns the zero value of each result
func nothing(int n) -> int, str:
  if n > 100:
    return n, "big"

# Results can feed each other: the controller pattern (state in, new state out)
func step(float acc, int k) -> float, int:
  return acc + k * 0.5, k + 1

state main:
  on go(n):
    count, total, label = split(n)
    log(label, total)
    # an int result into a float variable
    float half = 0
    half, label = nothing(n)
    log(label, half / 2)
    float acc = 0
    int k = 0
    while k < n:
      acc, k = step(acc, k)
    log("acc", acc)
    # results assigned to a float from an int result
    total, k = step(total, count)
    log("total", total)
