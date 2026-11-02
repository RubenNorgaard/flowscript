# time values: construction, parts, arithmetic, text, the clock
flow times version 1

extern show(time t)
extern parts(int y, int m, int d, int h, int mi, int s)
extern info(int ms, int wd)
extern text(str s)

event at(time t)
event parse(str s)
event make(int y, int m, int d)

const EPOCH = time(1970, 1, 1)
const NEWYEAR = time("2026-01-01T00:00:00Z")

time last = time(2000, 2, 29, 12, 30, 15)

state main:
  on boot:
    show(EPOCH)
    show(NEWYEAR + 1500)
    show(last)
    show(now())
    show(date(last))
    info(last - date(last), weekday(last))
    text(str(NEWYEAR - 1))
    info(NEWYEAR - EPOCH, weekday(EPOCH))
    info(EPOCH - NEWYEAR, 0)

  on at(t):
    show(t)
    parts(year(t), month(t), day(t), hour(t), minute(t), second(t))
    info(millisecond(t), weekday(t))
    info(t - last, int(float(t) - float(last)))
    show(now())
    last = t

  on parse(s):
    show(time(s))

  on make(y, m, d):
    show(time(y, m, d))
