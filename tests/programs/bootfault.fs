# A fault in boot aborts it and clears the pending initial transition, so
# the initial state is never entered (only global handlers run).
flow bootfault

extern log(x)

event ev(d)

var z = 0

any_state:
  on boot:
    log(1)
    log(1 / z)
    log(2)

  on ev(d):
    log(100 / d)

state first:
  on enter:
    log(10)

  on ev(d):
    log(11)
