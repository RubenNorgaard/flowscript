# Log statements: the VM hands the host the message template and the values; the host
# renders them (here the runners' traces: log LEVEL "message").

event go(int n, str who)

const LIMIT = 0.75

float level = 0.5
time since = time(2026, 11, 1)
str name = "pid"

# functions may log too (it changes no variable)
func checked(float x) -> float:
  if x > LIMIT:
    warning "{x} exceeds the limit of {LIMIT}, clamped"
    return LIMIT
  return x

initial state main:
  on boot:
    debug "booting {name}"
    info "started at {since}, level {level}"

  on go(n, who):
    level = checked(level + n * 0.125)
    info "{who} asked for {n}: level is now {level}"
    if n > 3:
      error "too much: {n} > 3 (from {who}, {name + \"/\" + who})"
    # braces, an empty message and eight values
    info "{{literal}} braces {{ and }}"
    debug ""
    info "{n}{n + 1}{n + 2}{n + 3}{n + 4}{n + 5}{n + 6}{n + 7}"
    info "quote \" and newline \n in the text, {\"and in \" + who}"
