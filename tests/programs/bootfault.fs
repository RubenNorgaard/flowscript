# A fault in boot aborts it (the rest of boot does not run); the initial state
# is still entered and handles events.
flow bootfault

extern log(int x) -> int

event ev(int d)

int z = 0

state first:
  on boot:
    log(1)
    log(1 / z)
    log(2)

  on enter:
    log(10)

  on ev(d):
    log(11)
