# GPS tracker logic, downloaded to the device as bytecode.
# The firmware (C) provides the externs and raises the events.

flow tracker version 3

# --- host functions provided by the firmware ---
extern gps_has_fix() -> int
extern gps_speed() -> int
extern battery_mv() -> int
extern send_position(int reason)
extern set_led(int pattern)
extern log(str msg, int value)

# --- events raised by the firmware ---
event ignition(int active)
event motion(int level)
event button
# --- events raised by the script itself ---
event no_fix
event report_sent
# tells the host to sound the overspeed alarm (buzzer, LED); the script does not handle it
event overspeed_alarm

timer report
timer fix_timeout

const LIMIT = 90
const LOW_BATTERY = 3500
# motion level that counts as theft if the GPS shows speed
const MOTION_WHILE_MOVING = 3
# motion level that counts as theft on its own
const MOTION_ALWAYS = 8
# consecutive overspeed reports before the alarm
const OVERSPEED_ALARM = 3
const REASON_PERIODIC = 1
const REASON_ALARM = 2
const REASON_THEFT = 3

int reports = 0

any_state:
  on boot:
    log("tracker logic started", 3)
    set_led(1)

  # Started from any_state, so the timer keeps running when the state changes
  on no_fix:
    start fix_timeout 2min

  on fix_timeout:
    log("no gps fix", 0)

  # Functions cannot change variables, so send_report announces each report
  on report_sent:
    reports = reports + 1

# Battery check is shared by every state
func battery_ok() -> int:
  return battery_mv() >= LOW_BATTERY

func send_report(int reason):
  if gps_has_fix():
    send_position(reason)
    raise_event report_sent
  else:
    raise_event no_fix

initial state parked:
  on enter:
    set_led(0)
    start report every 1h

  on ignition(active):
    if active:
      goto_state driving

  # Movement without ignition: possible theft
  on motion(level):
    if level > MOTION_WHILE_MOVING and not gps_speed() == 0 or level >= MOTION_ALWAYS:
      goto_state theft

  on report:
    if battery_ok():
      send_report(REASON_PERIODIC)

state driving:
  # Belongs to driving: starts at 0 every time the state is entered
  int overspeed_count = 0

  on enter:
    set_led(2)
    start report every 30s

  on report:
    int speed = gps_speed()
    if speed > LIMIT:
      overspeed_count = overspeed_count + 1
      if overspeed_count >= OVERSPEED_ALARM:
        send_report(REASON_ALARM)
        raise_event overspeed_alarm
    elif speed > LIMIT / 2 xor not battery_ok():
      send_report(REASON_PERIODIC)
    else:
      overspeed_count = 0

  on ignition(active):
    if not active:
      goto_state parked

  on exit:
    send_report(REASON_PERIODIC)

state theft:
  on enter:
    set_led(3)
    start report every 10s

  on report:
    send_report(REASON_THEFT)

  on button:
    goto_state parked
