# Handlers with more locals than the stack has slots (64).  The reference VM
# builds the oversized frame anyway and faults (stack overflow) on the first
# push.  Instructions that do not push (raise_event without args, stop, goto, return,
# call of an extern without args - until its result is pushed) still run.
flow bighandler

extern ping() -> int
extern show(int x) -> int

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
    int x00 = 1
    int x01 = 1
    int x02 = 1
    int x03 = 1
    int x04 = 1
    int x05 = 1
    int x06 = 1
    int x07 = 1
    int x08 = 1
    int x09 = 1
    int x10 = 1
    int x11 = 1
    int x12 = 1
    int x13 = 1
    int x14 = 1
    int x15 = 1
    int x16 = 1
    int x17 = 1
    int x18 = 1
    int x19 = 1
    int x20 = 1
    int x21 = 1
    int x22 = 1
    int x23 = 1
    int x24 = 1
    int x25 = 1
    int x26 = 1
    int x27 = 1
    int x28 = 1
    int x29 = 1
    int x30 = 1
    int x31 = 1
    int x32 = 1
    int x33 = 1
    int x34 = 1
    int x35 = 1
    int x36 = 1
    int x37 = 1
    int x38 = 1
    int x39 = 1
    int x40 = 1
    int x41 = 1
    int x42 = 1
    int x43 = 1
    int x44 = 1
    int x45 = 1
    int x46 = 1
    int x47 = 1
    int x48 = 1
    int x49 = 1
    int x50 = 1
    int x51 = 1
    int x52 = 1
    int x53 = 1
    int x54 = 1
    int x55 = 1
    int x56 = 1
    int x57 = 1
    int x58 = 1
    int x59 = 1
    int x60 = 1
    int x61 = 1
    int x62 = 1
    int x63 = 1
    int x64 = 1
    int x65 = 1
    int x66 = 1
    int x67 = 1
    show(x67)

  on big2:
    goto_state other
    int y00 = 1
    int y01 = 1
    int y02 = 1
    int y03 = 1
    int y04 = 1
    int y05 = 1
    int y06 = 1
    int y07 = 1
    int y08 = 1
    int y09 = 1
    int y10 = 1
    int y11 = 1
    int y12 = 1
    int y13 = 1
    int y14 = 1
    int y15 = 1
    int y16 = 1
    int y17 = 1
    int y18 = 1
    int y19 = 1
    int y20 = 1
    int y21 = 1
    int y22 = 1
    int y23 = 1
    int y24 = 1
    int y25 = 1
    int y26 = 1
    int y27 = 1
    int y28 = 1
    int y29 = 1
    int y30 = 1
    int y31 = 1
    int y32 = 1
    int y33 = 1
    int y34 = 1
    int y35 = 1
    int y36 = 1
    int y37 = 1
    int y38 = 1
    int y39 = 1
    int y40 = 1
    int y41 = 1
    int y42 = 1
    int y43 = 1
    int y44 = 1
    int y45 = 1
    int y46 = 1
    int y47 = 1
    int y48 = 1
    int y49 = 1
    int y50 = 1
    int y51 = 1
    int y52 = 1
    int y53 = 1
    int y54 = 1
    int y55 = 1
    int y56 = 1
    int y57 = 1
    int y58 = 1
    int y59 = 1
    int y60 = 1
    int y61 = 1
    int y62 = 1
    int y63 = 1
    int y64 = 1
    int y65 = 1
    int y66 = 1
    int y67 = 1
    int y68 = 1
    int y69 = 1

  on big3:
    if 0:
      int z00 = 1
      int z01 = 1
      int z02 = 1
      int z03 = 1
      int z04 = 1
      int z05 = 1
      int z06 = 1
      int z07 = 1
      int z08 = 1
      int z09 = 1
      int z10 = 1
      int z11 = 1
      int z12 = 1
      int z13 = 1
      int z14 = 1
      int z15 = 1
      int z16 = 1
      int z17 = 1
      int z18 = 1
      int z19 = 1
      int z20 = 1
      int z21 = 1
      int z22 = 1
      int z23 = 1
      int z24 = 1
      int z25 = 1
      int z26 = 1
      int z27 = 1
      int z28 = 1
      int z29 = 1
      int z30 = 1
      int z31 = 1
      int z32 = 1
      int z33 = 1
      int z34 = 1
      int z35 = 1
      int z36 = 1
      int z37 = 1
      int z38 = 1
      int z39 = 1
      int z40 = 1
      int z41 = 1
      int z42 = 1
      int z43 = 1
      int z44 = 1
      int z45 = 1
      int z46 = 1
      int z47 = 1
      int z48 = 1
      int z49 = 1
      int z50 = 1
      int z51 = 1
      int z52 = 1
      int z53 = 1
      int z54 = 1
      int z55 = 1
      int z56 = 1
      int z57 = 1
      int z58 = 1
      int z59 = 1
      int z60 = 1
      int z61 = 1
      int z62 = 1
      int z63 = 1

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
