# no states, no scenario file: boot only
extern log(int x) -> int

int g = -5

state main:
  on boot:
    log(g)
    g = g * 3
