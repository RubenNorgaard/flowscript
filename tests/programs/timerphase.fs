# Timers across transitions.  A state's timers stop when it is left, after the new
# state's 'on enter' has run: a periodic timer that 'on enter' starts again with the
# same period keeps running, phase and all.  A one-shot restart always resets it.

extern log(str what, int value)

event go
event next
event kick
event self

timer beat
timer watchdog
timer early

int beats = 0

initial state a:
  on boot:
    # belongs to the initial state: it stops if the program starts somewhere else
    start early every 50
    goto_state b

  on enter:
    start beat every 100

  on beat:
    beats = beats + 1
    log("a beat", beats)

  on early:
    log("early", 0)

  on go:
    goto_state b

  on self:
    goto_state a

state b:
  on enter:
    log("enter b", beats)
    start beat every 100

  on beat:
    beats = beats + 1
    log("b beat", beats)

  on kick:
    # one-shot: every kick pushes the deadline out again
    start watchdog 150

  on watchdog:
    log("watchdog", beats)

  on next:
    goto_state c

  on go:
    goto_state a

# c starts beat with another period: a restart, a new phase
state c:
  on enter:
    log("enter c", beats)
    start beat every 70

  on beat:
    beats = beats + 1
    log("c beat", beats)

  on go:
    goto_state b
