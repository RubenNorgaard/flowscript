# no states, no scenario file: boot only
extern log(int x) -> int

int g = -5

any_state:
  on boot:
    log(g)
    g = g * 3
