flow strings version 42

extern say(s, n)

event hello

any_state:
  on boot:
    say("hello", 1)
    say("world", 2)
    say("hello", 3)

  on hello:
    say("again\n\t\"quoted\"", 4)
