# Faults: abort the handler, report pc, continue with the next event
flow faults

extern ping(x)
extern wide(p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12, p13, p14, p15)

event recurse(n)
event depth(n)
event spin
event almost
event deep
event many
event after
event widecall

var counter = 0
var v = 1

func rec(n):
  return rec(n + 1)

func down(n):
  if n > 0:
    return down(n - 1) + 1
  return 0

func lots():
  var l00 = 0
  var l01 = 0
  var l02 = 0
  var l03 = 0
  var l04 = 0
  var l05 = 0
  var l06 = 0
  var l07 = 0
  var l08 = 0
  var l09 = 0
  var l10 = 0
  var l11 = 0
  var l12 = 0
  var l13 = 0
  var l14 = 0
  var l15 = 0
  var l16 = 0
  var l17 = 0
  var l18 = 0
  var l19 = 0
  var m00 = 0
  var m01 = 0
  var m02 = 0
  var m03 = 0
  var m04 = 0
  var m05 = 0
  var m06 = 0
  var m07 = 0
  var m08 = 0
  var m09 = 0
  return 7

func lots_rec(n):
  var a = n
  var b = n
  var c = n
  if n > 0:
    return lots_rec(n - 1)
  return lots()

any_state:
  on recurse(n):
    counter = 100
    ping(rec(n))
    counter = 101

  on depth(n):
    ping(down(n))

  on spin:
    counter = 0
    while 1:
      counter = counter + 1

  # a loop that just fits into the budget of 10000 instructions
  on almost:
    var i = 0
    while i < 1110:
      i = i + 1
    ping(i)

  on deep:
    counter = v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + (v + 1)))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))

  on many:
    ping(lots())
    ping(lots_rec(2))
    ping(lots_rec(3))
    ping(lots_rec(4))
    ping(lots_rec(5))
    ping(lots_rec(6))
    ping(lots_rec(1))

  on widecall:
    counter = 5
    # 15 args pending per level -> > 64 stack slots
    wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, wide(v, v, v, v, v, v, v, v, v, v, v, v, v, v, v, v)))))

  on after:
    ping(counter)
