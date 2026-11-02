# Host-managed timers: the VM asks the host to start/stop timers and the host
# reports expiries with fs_timer_expired() (scenario: host_timers + fire).

extern log(int x) -> int

event go
event halt

timer beat
timer once
timer local

initial state idle:
  on boot:
    start beat every 1s

  on enter:
    start once 500
    start local every 200

  on once:
    log(2)

  on local:
    log(3)

  on go:
    goto_state busy

  on beat:
    log(1)

  on halt:
    stop beat

state busy:
  on enter:
    start once 100

  on once:
    log(4)
    goto_state idle

  on beat:
    log(1)

  on halt:
    stop beat
