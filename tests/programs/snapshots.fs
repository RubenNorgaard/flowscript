# Snapshots: state, globals of every kind (an image string, a run-time string, the
# empty string, float, time) and running timers survive snapshot -> restore; the
# run after a restore is the run after the snapshot.

extern say(str what)

event go(str who)
event next

timer beat
timer once

int beats = 0
float level = 0.5
time since = time(2026, 11, 1)
str label = "start"
str who_last = ""

initial state idle:
  on boot:
    label = "booted"

  on go(who):
    who_last = who + "!"
    level = level * 3
    goto_state busy

state busy:
  int seen = 0

  on enter:
    start beat every 100
    start once 250

  on beat:
    beats = beats + 1
    seen = seen + 1
    say(who_last + " " + str(beats))

  on once:
    info "once after {beats} beats, level {level}, since {since}"

  on next:
    goto_state idle
