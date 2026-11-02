# Timers: one-shot, periodic, restart, stop, catch-up with large ticks
flow timers

extern fired(int id, int n) -> int

event cmd(int op, int ms)

timer once
timer per
timer zero
timer neg

int n_once = 0
int n_every = 0
int n_zero = 0

state main:
  on cmd(op, ms):
    if op == 1:
      start once ms
    elif op == 2:
      start per every ms
    elif op == 3:
      stop once
    elif op == 4:
      stop per
    elif op == 5:
      start zero every ms
    elif op == 6:
      stop zero
    elif op == 7:
      start neg ms

  on once:
    n_once = n_once + 1
    fired(1, n_once)

  on per:
    n_every = n_every + 1
    fired(2, n_every)
    if n_every == 20:
      stop per

  on zero:
    n_zero = n_zero + 1
    fired(3, n_zero)

  on neg:
    fired(4, 0)
    # restart from inside its own handler
    start neg 50
