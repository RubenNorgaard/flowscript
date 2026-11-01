# goto_state semantics: from boot, from functions, inside exit (ignored), in enter
flow transitions

extern log(int x) -> int

event go(int n)
event poke

int path = 0

func jump(int n) -> int:
  log(100 + n)
  if n == 1:
    goto_state b
  if n == 2:
    goto_state c
  return n

any_state:
  on boot:
    log(1)
    # selects the initial state instead of the first declared one
    goto_state b
    log(999)

  on poke:
    log(50)

initial state a:
  on enter:
    log(10)

  on exit:
    log(11)
    # ignored
    goto_state c
    log(999)

  on go(n):
    log(jump(n))
    log(12)

state b:
  on enter:
    log(20)
    path = path + 1

  on exit:
    log(21)
    # goto_state inside a function inside exit: also ignored
    jump(2)
    log(999)

  on go(n):
    if n == 0:
      goto_state a
    elif n == 5:
      # self transition: exit + enter run again
      goto_state b
    elif n == 6:
      raise_event poke
      goto_state c
    else:
      log(jump(n))

state c:
  on enter:
    log(30)
    if path < 3:
      # goto_state from enter
      goto_state b

  on go(n):
    goto_state a

  # state handler overrides the global one
  on poke:
    log(31)
