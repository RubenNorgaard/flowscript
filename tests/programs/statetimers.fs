# Timers started by a state's handlers (or functions they call) stop when the
# state is left, also the ones its 'on boot' and shared handlers start.

extern log(int x) -> int

event go(int to)
event arm

timer blink
timer slow
timer helper
timer late
timer keep

func arm_helper():
  start helper every 300

initial state a:
  on boot:
    start keep every 250

  on enter:
    start blink every 100

  on blink:
    log(1)

  on slow:
    log(2)

  on helper:
    log(3)

  on go(to):
    arm_helper()
    if to == 1:
      goto_state b
    elif to == 2:
      goto_state a

  on exit:
    start late 50

  on arm:
    start slow every 100

  on keep:
    log(9)

state b:
  on blink:
    log(10)

  on slow:
    log(20)

  on helper:
    log(30)

  on late:
    log(40)

  on go(to):
    goto_state a

  on arm:
    start slow every 100

  on keep:
    log(9)
