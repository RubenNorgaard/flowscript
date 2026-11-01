# float arithmetic, promotion, formatting and math functions
flow floats version 1

extern show(float x)
extern showi(int x)
extern shows(str s)

event calc(float a, float b)

const PI = 3.141592653589793

float total = 0
float big = 1e300

any_state:
  on boot:
    show(0.1 + 0.2)
    show(1 / 3.0)
    show(2 / 3)
    show(-0.0)
    show(1e20)
    show(1e-7)
    show(123456789.123)
    show(PI * 2)
    show(big * big)
    show(-big * big)
    show(5 % 3.5)
    show(sqrt(2))
    show(exp(1))
    show(log(10))
    show(pow(2, 0.5))
    show(floor(-2.5))
    show(ceil(-2.5))
    show(round(2.5))
    show(round(-2.5))
    show(round(0.49999999999999994))
    show(abs(-4.25))
    show(min(3, 2.5))
    show(max(3, 2.5))
    showi(int(2.9))
    showi(int(-2.9))
    shows(str(1 / 7.0))
    shows(str(1e15))
    shows(str(100000000000000.0))
    shows(str(0.0001))
    shows(str(0.00001))
    showi(1.5 < 2)
    showi(1.0 == 1)

  on calc(a, b):
    total = total + a / b
    show(total)
    show(a * b)
    show(a - b)
    showi(int(a / b))
