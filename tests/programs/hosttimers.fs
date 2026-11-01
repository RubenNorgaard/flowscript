# Host-managed timers: the VM asks the host to start/stop timers and the host
# reports expiries with fs_timer_expired() (scenario: host_timers + fire).

extern log(x)

event go
event halt

timer beat
timer once
timer local

any_state:
  on boot:
    start beat every 1s

  on beat:
    log(1)

  on halt:
    stop beat

initial state idle:
  on enter:
    start once 500
    start local every 200

  on once:
    log(2)

  on local:
    log(3)

  on go:
    goto_state busy

state busy:
  on enter:
    start once 100

  on once:
    log(4)
    goto_state idle
