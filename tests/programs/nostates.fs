# no states, no scenario file: boot only
extern log(x)

var g = -5

any_state:
  on boot:
    log(g)
    g = g * 3
