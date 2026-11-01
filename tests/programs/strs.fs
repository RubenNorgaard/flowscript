# strings: concatenation, slots and their collection, conversions, too long, out of slots
flow strs version 1

extern say(str s)
extern sayi(int n)
extern name() -> str

event add(str piece)
event fill(int n)
event convert(str text)
event hoard(int n)

str acc = ""
str keep = "const"

any_state:
  on boot:
    say("a" + "b")
    say(keep + "!" + str(42))
    sayi(len("hello"))
    say(substr("hello world", 6, 5))
    say(substr("hello", -3, 2))
    say(substr("hello", 3, 99))
    sayi(find("hello", "ll"))
    sayi(find("hello", "z"))
    sayi(find("hello", ""))
    sayi("abc" < "abd")
    sayi("ab" < "abc")
    sayi("b" > "abc")
    sayi("x" == "x")
    say(str(-7) + str(2.5) + str(time(2026, 11, 1)))
    say(name() + "?")

  on add(piece):
    acc = acc + piece
    say(acc)
    sayi(len(acc))

  on fill(n):
    str a = "x" + str(n)
    str b = a + a
    str c = b + b
    while n > 0:
      say(str(n))
      n = n - 1
    say(a + b + c)

  # more live strings than the 16 slots: fault 11 (they are freed once the handler ends)
  on hoard(n):
    str s1 = str(n + 1)
    str s2 = str(n + 2)
    str s3 = str(n + 3)
    str s4 = str(n + 4)
    str s5 = str(n + 5)
    str s6 = str(n + 6)
    str s7 = str(n + 7)
    str s8 = str(n + 8)
    str s9 = str(n + 9)
    str s10 = str(n + 10)
    str s11 = str(n + 11)
    str s12 = str(n + 12)
    str s13 = str(n + 13)
    str s14 = str(n + 14)
    str s15 = str(n + 15)
    say(s1 + s15)
    str s16 = str(n + 16)
    say("not reached")

  on convert(text):
    sayi(int(text))
    show_float(text)

func show_float(str t) -> int:
  say(str(float(t)))
  return 0
