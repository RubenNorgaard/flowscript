"""Source -> model -> source round trip must be stable (the basis of visual editing)."""
import glob, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flowscript import parse, to_source, compile_source

ROOT = os.path.join(os.path.dirname(__file__), "..")

TRICKY = '''
# header comment

flow t version 2

# standalone comment

# note for extern
extern f(int a, str b) -> int  # trailing
event e(int x)
timer tm
const K = -(3 + 4) * 2
const M = 1 << 4 | 2
int g = K
int h = 0x10

initial state s1:
  # a note on a comment-only line before handler

  on enter:
    # standalone in block

    g = (1 + 2) * 3 - -4
    g = 1 - (2 - 3)
    g = not (g > 1 and h < 2) or g == 3 xor true
    g = ~g & 0b1010 ^ 5 % 2
    start tm every 30s
    start tm 2min
    if g:
      f(1, "str \\"q\\"")
    elif h:
      # comment at end of block
      pass
    # comment directly above else stays in the elif body
    else:
      while g < 10:
        g = g + 1
      # note for the statement after the loop
      g = 0
  on e(y):
    goto_state s1

state empty:
  pass

func fn(int a) -> int:
  int b = a * 2
  return b
'''


class RoundTrip(unittest.TestCase):
    def check(self, src):
        once = to_source(parse(src))
        twice = to_source(parse(once))
        self.assertEqual(once, twice)
        compile_source(once)
        return once

    def test_tricky(self):
        out = self.check(TRICKY)
        self.assertIn("# standalone in block\n\n", out)
        self.assertIn("const K = -(3 + 4) * 2", out)
        self.assertIn("g = 1 - (2 - 3)", out)
        self.assertIn("start tm every 30s", out)
        self.assertIn("    elif h:\n      # comment at end of block\n\n      # comment directly above else stays in the elif body\n\n      pass\n    else:", out)
        self.assertIn("      # note for the statement after the loop\n      g = 0", out)
        self.assertIn("state empty:\n  pass\n", out)

    def test_ast_shape(self):
        ast = parse(TRICKY)
        s1 = [i for i in ast["items"] if i["type"] == "state"][0]
        enter = s1["handlers"][1]
        if_ = [s for s in enter["body"] if s["type"] == "if"][0]
        self.assertEqual([c["type"] for c in if_["branches"][1]["body"]], ["comment", "comment"])
        last = if_["else_body"][-1]
        self.assertEqual((last["type"], last["note"]), ("assign", "note for the statement after the loop"))

    def test_examples(self):
        files = glob.glob(os.path.join(ROOT, "examples", "*.fs")) + glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs"))
        self.assertTrue(files)
        for fn in files:
            with open(fn) as f:
                src = f.read()
            with self.subTest(fn=os.path.basename(fn)):
                self.check(src)

    def test_semantics_preserved(self):
        from flowscript.runner import run_scenario
        for fn in glob.glob(os.path.join(ROOT, "examples", "*.fs")):
            sc = fn[:-3] + ".scenario"
            if not os.path.exists(sc):
                continue
            with open(fn) as f:
                src = f.read()
            with open(sc) as f:
                scenario = f.read()
            a, b = [], []
            run_scenario(compile_source(src)[1], scenario, a.append)
            run_scenario(compile_source(to_source(parse(src)))[1], scenario, b.append)
            self.assertEqual(a, b)


TYPED = '''flow typed version 2

extern f(int a, float b, str c, time d) -> str
extern log(str s)
event e(float x, str label)

const RATE = 2.5e-3
const START = time(2026, 1, 1)
float total = -0.5
str name = "a \\"b\\""
time when

func scale(float x, int n) -> float:
  float y = x * n + RATE
  return y

state s:
  int count = 0
  time last

  on e(x, label):
    str t = f(1, x, label, now())
    log(t + str(1.0e10) + name)
'''


class Typed(unittest.TestCase):
    def test_typed_round_trip(self):
        once = to_source(parse(TYPED))
        self.assertEqual(once, to_source(parse(once)))
        self.assertIn("extern f(int a, float b, str c, time d) -> str", once)
        self.assertIn("func scale(float x, int n) -> float:", once)
        self.assertIn("  float y = x * n + RATE", once)
        self.assertIn("  time last", once)
        self.assertIn("const RATE = 2.5e-3", once)
        self.assertIn("float total = -0.5", once)
        compile_source(once)


if __name__ == "__main__":
    unittest.main()
