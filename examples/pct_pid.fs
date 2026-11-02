# pct_pid: market making whose aggression is steered by a PID loop toward a daily
# profit target (% of the capital allocated to it). A port of bitmaker's
# strategies/pid_target.py; the inventory-skew quoting underneath is the host's.
flow pct_pid version 3

# --- the host (bitmaker) provides ---
# a runner setting (all numeric settings are floats)
extern param(str name) -> float
# the runner's P&L today (UTC), fees included, in the settlement currency
extern pnl_today() -> float
# value of one lot at the fair price; 0 = no price yet
extern lot_value() -> float
# the quoting underneath (inventory skew) for this tick
extern skew_quotes(float floor_bps, float size_lots, float max_lots, int reduce_only)
extern diag(str name, float value)
extern refuse(str reason)

# the engine's quote tick
event tick
event params_changed

timer control

const HOUR_MS = 3600000.0
const DAY_MS = 86400000.0

# the controller's memory (only handlers change it: control_step returns the new values)
time day
time last
float integral = 0
float last_error = 0
float deriv = 0
float aggression = 0.5
# loss today, % of capital (the states decide on it)
float loss_pct = 0

func clamp(float x, float lo, float hi) -> float:
  return min(max(x, lo), hi)

# One tick of quoting at aggression a: inventory skew with a steered spread floor,
# size and position cap.
func quote(float a, int reduce_only):
  float lo = param("min_half_spread_bps")
  float hi = param("max_half_spread_bps")
  float floor_bps = hi + (lo - hi) * a
  float full = param("size")
  float frac = param("min_size_frac")
  float size = full * (frac + (1 - frac) * a)
  # never below one lot (a fraction of a lot rounds to nothing)
  if full >= 1 and size < 1:
    size = 1
  float cap = param("max_inventory")
  if lot_value() > 0:
    cap = min(cap, param("capital") * param("max_capital_use") / lot_value())
  # capital too small for a single quote at this size
  if cap < size:
    size = max(cap, 0)
  diag("spread_floor_bps", floor_bps)
  diag("size_lots", size)
  if size > 0:
    skew_quotes(floor_bps, size, cap, reduce_only)

# One controller update (pid_target.py: _control), from the controller's memory to its
# new memory.  hold: braking or stopped, so the aggression is 0 and nothing is integrated.
func control_step(time t, time day, time last, float integral, float last_error, float deriv, int hold) -> time, float, float, float, float, float:
  if date(t) != day:
    # a new UTC day: a fresh target, a fresh integral
    day = date(t)
    integral = 0
    deriv = 0
    last = day
  float capital = param("capital")
  float daily_target = capital * param("target_pct_per_day") / 100
  float target = daily_target * (t - day) / DAY_MS
  float daily = pnl_today()
  # > 0 behind, < 0 ahead; 1 = a whole day's target behind
  float error = (target - daily) / daily_target
  float hours = (t - last) / HOUR_MS
  # smoothed derivative: P&L jumps with every fill
  if hours > 0:
    deriv = deriv + 0.3 * ((error - last_error) / hours - deriv)
  float ki = param("ki")
  float p = param("kp") * error
  float d = param("kd") * deriv
  float out = param("base_aggression") + p + ki * integral + d
  # anti-windup: don't integrate into a pinned output, nor while holding
  int pinned = out >= 1 and error > 0 or out <= 0 and error < 0
  if hours > 0 and not hold and not pinned:
    integral = integral + error * hours
    if ki > 0:
      integral = clamp(integral, -1 / ki, 1 / ki)
  float level = clamp(param("base_aggression") + p + ki * integral + d, 0, 1)
  if hold:
    level = 0
  diag("aggression", level)
  diag("error", error)
  diag("pnl_today", daily)
  return day, integral, error, deriv, level, -daily / capital * 100

func update_ms() -> int:
  return int(param("update_secs") * 1000)

func valid() -> int:
  if param("capital") <= 0 or param("target_pct_per_day") <= 0 or param("update_secs") <= 0:
    refuse("capital, target_pct_per_day and update_secs must be positive")
    return false
  if param("base_aggression") > 1 or param("min_size_frac") > 1:
    refuse("base_aggression and min_size_frac must be between 0 and 1")
    return false
  return true

initial state trading:
  on boot:
    aggression = param("base_aggression")

  # every state starts the controller with the same period: it keeps its rhythm
  on enter:
    start control every update_ms()

  on control:
    time t = now()
    day, integral, last_error, deriv, aggression, loss_pct = control_step(t, day, last, integral, last_error, deriv, false)
    last = t
    if loss_pct >= param("stop_pct"):
      error "lost {loss_pct}% of capital today: stopped, only reducing the position"
      goto_state stopped
    elif loss_pct >= param("brake_pct"):
      warning "lost {loss_pct}% of capital today: braking (aggression 0)"
      goto_state braking

  on tick:
    quote(aggression, false)

  # settings changed on the desk: what the controller has measured is kept
  on params_changed:
    if valid():
      start control every update_ms()

# lost brake_pct today: aggression held at 0 (wide and small)
state braking:
  on enter:
    start control every update_ms()

  on control:
    time t = now()
    day, integral, last_error, deriv, aggression, loss_pct = control_step(t, day, last, integral, last_error, deriv, true)
    last = t
    if loss_pct >= param("stop_pct"):
      error "lost {loss_pct}% of capital today: stopped, only reducing the position"
      goto_state stopped
    elif loss_pct < param("brake_pct"):
      info "loss {loss_pct}% of capital: back to trading"
      goto_state trading

  on tick:
    quote(aggression, false)

  on params_changed:
    if valid():
      start control every update_ms()

# lost stop_pct today: only the side that reduces the position
state stopped:
  on enter:
    start control every update_ms()

  on control:
    time t = now()
    day, integral, last_error, deriv, aggression, loss_pct = control_step(t, day, last, integral, last_error, deriv, true)
    last = t
    # a new day starts from zero: back to braking or trading
    if loss_pct < param("brake_pct"):
      info "loss {loss_pct}% of capital: back to trading"
      goto_state trading
    elif loss_pct < param("stop_pct"):
      info "loss {loss_pct}% of capital: braking again instead of stopped"
      goto_state braking

  on tick:
    quote(0, true)

  on params_changed:
    if valid():
      start control every update_ms()
