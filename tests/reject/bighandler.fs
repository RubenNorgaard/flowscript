# Handlers with more locals than the stack has slots (64).  The reference VM
# builds the oversized frame anyway and faults (stack overflow) on the first
# push.  Instructions that do not push (raise_event without args, stop, goto, return,
# call of an extern without args - until its result is pushed) still run.
flow bighandler

extern ping()
extern show(x)

event big
event big2
event big3
event notify
event after

timer t

initial state idle:
  on enter:
    start t every 100

  on big:
    raise_event notify
    stop t
    ping()
    var x00 = 1
    var x01 = 1
    var x02 = 1
    var x03 = 1
    var x04 = 1
    var x05 = 1
    var x06 = 1
    var x07 = 1
    var x08 = 1
    var x09 = 1
    var x10 = 1
    var x11 = 1
    var x12 = 1
    var x13 = 1
    var x14 = 1
    var x15 = 1
    var x16 = 1
    var x17 = 1
    var x18 = 1
    var x19 = 1
    var x20 = 1
    var x21 = 1
    var x22 = 1
    var x23 = 1
    var x24 = 1
    var x25 = 1
    var x26 = 1
    var x27 = 1
    var x28 = 1
    var x29 = 1
    var x30 = 1
    var x31 = 1
    var x32 = 1
    var x33 = 1
    var x34 = 1
    var x35 = 1
    var x36 = 1
    var x37 = 1
    var x38 = 1
    var x39 = 1
    var x40 = 1
    var x41 = 1
    var x42 = 1
    var x43 = 1
    var x44 = 1
    var x45 = 1
    var x46 = 1
    var x47 = 1
    var x48 = 1
    var x49 = 1
    var x50 = 1
    var x51 = 1
    var x52 = 1
    var x53 = 1
    var x54 = 1
    var x55 = 1
    var x56 = 1
    var x57 = 1
    var x58 = 1
    var x59 = 1
    var x60 = 1
    var x61 = 1
    var x62 = 1
    var x63 = 1
    var x64 = 1
    var x65 = 1
    var x66 = 1
    var x67 = 1
    show(x67)

  on big2:
    goto_state other
    var y00 = 1
    var y01 = 1
    var y02 = 1
    var y03 = 1
    var y04 = 1
    var y05 = 1
    var y06 = 1
    var y07 = 1
    var y08 = 1
    var y09 = 1
    var y10 = 1
    var y11 = 1
    var y12 = 1
    var y13 = 1
    var y14 = 1
    var y15 = 1
    var y16 = 1
    var y17 = 1
    var y18 = 1
    var y19 = 1
    var y20 = 1
    var y21 = 1
    var y22 = 1
    var y23 = 1
    var y24 = 1
    var y25 = 1
    var y26 = 1
    var y27 = 1
    var y28 = 1
    var y29 = 1
    var y30 = 1
    var y31 = 1
    var y32 = 1
    var y33 = 1
    var y34 = 1
    var y35 = 1
    var y36 = 1
    var y37 = 1
    var y38 = 1
    var y39 = 1
    var y40 = 1
    var y41 = 1
    var y42 = 1
    var y43 = 1
    var y44 = 1
    var y45 = 1
    var y46 = 1
    var y47 = 1
    var y48 = 1
    var y49 = 1
    var y50 = 1
    var y51 = 1
    var y52 = 1
    var y53 = 1
    var y54 = 1
    var y55 = 1
    var y56 = 1
    var y57 = 1
    var y58 = 1
    var y59 = 1
    var y60 = 1
    var y61 = 1
    var y62 = 1
    var y63 = 1
    var y64 = 1
    var y65 = 1
    var y66 = 1
    var y67 = 1
    var y68 = 1
    var y69 = 1

  on big3:
    if 0:
      var z00 = 1
      var z01 = 1
      var z02 = 1
      var z03 = 1
      var z04 = 1
      var z05 = 1
      var z06 = 1
      var z07 = 1
      var z08 = 1
      var z09 = 1
      var z10 = 1
      var z11 = 1
      var z12 = 1
      var z13 = 1
      var z14 = 1
      var z15 = 1
      var z16 = 1
      var z17 = 1
      var z18 = 1
      var z19 = 1
      var z20 = 1
      var z21 = 1
      var z22 = 1
      var z23 = 1
      var z24 = 1
      var z25 = 1
      var z26 = 1
      var z27 = 1
      var z28 = 1
      var z29 = 1
      var z30 = 1
      var z31 = 1
      var z32 = 1
      var z33 = 1
      var z34 = 1
      var z35 = 1
      var z36 = 1
      var z37 = 1
      var z38 = 1
      var z39 = 1
      var z40 = 1
      var z41 = 1
      var z42 = 1
      var z43 = 1
      var z44 = 1
      var z45 = 1
      var z46 = 1
      var z47 = 1
      var z48 = 1
      var z49 = 1
      var z50 = 1
      var z51 = 1
      var z52 = 1
      var z53 = 1
      var z54 = 1
      var z55 = 1
      var z56 = 1
      var z57 = 1
      var z58 = 1
      var z59 = 1
      var z60 = 1
      var z61 = 1
      var z62 = 1
      var z63 = 1

  on notify:
    show(42)

  on t:
    show(1)

state other:
  on big:
    ping()
    goto_state idle

  on after:
    show(7)
