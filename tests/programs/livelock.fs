# Scripts that never go idle are stopped after FS_MAX_CHAIN (256) dispatches
# with fault 9; the pending transition and the event queue are dropped.

extern log(x)

event pingpong
event spin
event chain(n)
event hello

var bounces = 0
var spins = 0

initial state idle:
  on pingpong:
    goto_state ping

  on spin:
    spins = spins + 1
    raise_event spin

  # a long but finite cascade must not trip the limit
  on chain(n):
    if n > 0:
      raise_event chain(n - 1)
    else:
      log(spins)

  on hello:
    log(bounces)

state ping:
  on enter:
    bounces = bounces + 1
    goto_state pong

  on hello:
    log(1000 + bounces)

state pong:
  on enter:
    goto_state ping
