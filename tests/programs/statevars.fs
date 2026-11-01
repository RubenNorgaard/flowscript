# State variables: visible only in their state, reset on every entry
# (a has no 'on enter', so the compiler adds one; b's own 'on enter' resets first)

extern log(str msg, int level)

event go
event bump

initial state a:
  # counts bumps in a
  int n = 5
  int m

  on bump:
    n = n + 1
    m = m + 2
    log("a", n + m)

  on go:
    goto_state b

state b:
  int n = 100

  on enter:
    log("b enter", n)

  on bump:
    n = n + 1
    log("b", n)

  on go:
    goto_state a
