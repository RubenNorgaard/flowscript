# Log statements in the smallest C VM (FS_NO_FLOAT FS_NO_TIME FS_NO_STR FS_NO_TEXT):
# only ints and string literals, and no string is made at run time.

event go(int n)

int count = 0

initial state idle:
  on go(n):
    count = count + n
    info "idle got {n}, count {count}"
    if count > 5:
      warning "count {count} is over {5}: {\"busy\"} now"
      goto_state busy

state busy:
  on enter:
    debug "entered busy"

  on go(n):
    error "busy: dropped {n}"
