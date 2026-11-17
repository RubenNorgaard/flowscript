"""The Python VM's compiled blocks (blocks.py) against plain interpretation.

Random bytecode (valid and not: wrong types, bad operands, deep stacks, loops into the
budget, calls, host calls, extreme values) runs on two VMs, one with blocks and one
interpreting every instruction; after each run(k), with k from 1 up, everything the host
can see must be identical: globals, stack, frames, pc, steps, faults, emits, the recording.
The example and test programs' scenarios must give identical traces and recordings too.

    FS_FUZZ=20000 python3 -m unittest tests.test_blocks     # a longer fuzz run
"""
import glob
import math
import os
import random
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
from flowscript import compile_source  # noqa: E402
from flowscript.bytecode import OPCODES, Program  # noqa: E402
from flowscript.runner import run_scenario  # noqa: E402
from flowscript.values import INT, FLOAT, STR, Str, Time  # noqa: E402
from flowscript.vm import VM  # noqa: E402

SIMPLE = ["NOP", "POP", "DUP", "ADD", "SUB", "MUL", "DIV", "MOD", "NEG", "BAND", "BOR", "BXOR",
          "BNOT", "SHL", "SHR", "EQ", "NE", "LT", "LE", "GT", "GE", "NOT", "BOOL", "CONCAT"]
INTS = [0, 1, -1, 2, 3, 7, -7, 31, 32, 33, 100, 2147483647, -2147483648, 65536, 46341, -46341]
FLOATS = [0.0, -0.0, 1.5, -2.25, 1e308, -1e308, 5e-324, math.inf, -math.inf, math.nan, 3.0]


def show(v):
    """A value with its exact type (True is not 1, -0.0 is not 0.0, nan is nan)."""
    return "%s:%r" % (type(v).__name__, v)


def ins(name, arg=None):
    code, kind = OPCODES[name]
    if kind is None:
        return bytes([code])
    fmt = {"i8": "<b", "u8": "<B", "u16": "<H", "i32": "<i", "f64": "<d", "i64": "<q"}[kind]
    return bytes([code]) + struct.pack(fmt, arg)


def random_program(rnd):
    """A program with one handler (event go(int, float)) and three functions, all random
    code: mostly well typed (a simulated type stack picks the instructions), sometimes not."""
    n = rnd.randint(4, 80)
    nglobals = 4
    nlocals = rnd.randint(2, 6)
    stk = []                                    # simulated types: i f s t
    loc = ["i", "f"] + ["i"] * (nlocals - 2)
    glob = ["i", "f", "s", "t"]
    items = []                                  # (name, arg); jump targets as ("@", index)

    def push_const():
        r = rnd.random()
        if r < 0.65:
            v = rnd.choice(INTS + [rnd.randint(-300, 300)] * 4)
            items.append(("PUSH8", v) if -128 <= v < 128 and rnd.random() < 0.7 else ("PUSH", v))
            stk.append("i")
        elif r < 0.9:
            items.append(("PUSHF", rnd.choice(FLOATS + [rnd.uniform(-100, 100)] * 4)))
            stk.append("f")
        elif r < 0.95:
            items.append(("PUSHT", rnd.randint(-10 ** 12, 10 ** 12)))
            stk.append("t")
        else:
            items.append(("PUSHS", rnd.randint(0, 3)))
            stk.append("s")

    def pop(k):
        for _ in range(k):
            if stk:
                stk.pop()

    for _ in range(n):
        top = stk[-1] if stk else None
        two = stk[-2:] if len(stk) >= 2 else None
        r = rnd.random()
        if r < 0.06:                            # anything at all
            name = rnd.choice(SIMPLE + ["LOADL", "STOREL", "LOADG", "STOREG", "BUILTIN", "BAD"])
            arg = {"LOADL": rnd.randint(0, 8), "STOREL": rnd.randint(0, 8), "LOADG": rnd.randint(0, 5),
                   "STOREG": rnd.randint(0, 5), "BUILTIN": rnd.choice([3, 5, 21, 25, 40])}.get(name)
            items.append((name, arg))
            stk = []                            # unknown from here
        elif r < 0.25 or not stk:
            push_const()
        elif r < 0.40:
            k = rnd.randrange(nlocals)
            items.append(("LOADL", k))
            stk.append(loc[k])
        elif r < 0.50:
            k = rnd.randrange(nlocals)
            items.append(("STOREL", k))
            loc[k] = stk.pop()
        elif r < 0.55:
            k = rnd.randrange(nglobals)
            items.append(("LOADG", k))
            stk.append(glob[k])
        elif r < 0.60:
            k = rnd.randrange(nglobals)
            items.append(("STOREG", k))
            glob[k] = stk.pop()
        elif r < 0.75 and two and two[0] == two[1] and two[0] in "if":
            ops = ["ADD", "SUB", "MUL", "DIV", "EQ", "NE", "LT", "LE", "GT", "GE"]
            if two[0] == "i":
                ops += ["MOD", "BAND", "BOR", "BXOR", "SHL", "SHR"]
            name = rnd.choice(ops)
            items.append((name, None))
            pop(2)
            stk.append("i" if name in ("EQ", "NE", "LT", "LE", "GT", "GE") else two[0])
        elif r < 0.80 and top in ("i", "f"):
            name = rnd.choice(["NEG", "BUILTIN"] + (["BNOT", "NOT", "BOOL", "DUP", "POP"] if top == "i" else ["DUP"]))
            if name == "BUILTIN":
                arg = rnd.choice([0, 24]) if top == "i" else 25
                items.append((name, arg))
                stk[-1] = "f" if arg in (0, 25) else "i"
            else:
                items.append((name, None))
                if name == "DUP":
                    stk.append(top)
                elif name == "POP":
                    stk.pop()
        elif r < 0.84 and two and two[0] == two[1] and two[0] in "if":
            items.append(("BUILTIN", rnd.choice([26, 27]) if two[0] == "i" else rnd.choice([28, 29])))
            pop(1)
        elif r < 0.91 and top == "i":
            items.append((rnd.choice(["JZ", "JNZ", "JNZ"]), ("@", rnd.randint(0, n))))
            stk.pop()
        elif r < 0.93:
            items.append(("JMP", ("@", rnd.randint(0, n))))
        elif r < 0.95:
            k = rnd.choice([0, 1, 2, 2, 3])          # 3: no such function
            items.append(("CALL", k))
            pop(k)
            stk.append("i")
        elif r < 0.97:
            items.append((rnd.choice(["RET", "RET0", "RETN"]), rnd.randint(0, 2)))
            pop(1)
        elif r < 0.985 and top == "i":
            items.append(("XCALL", 0))
        elif r < 0.99 and two == ["f", "f"]:
            items.append(("XCALL", 1))
            pop(1)
        else:
            items.append(("EMIT", 3))
            pop(1)
    items.append(("RET0", None))
    # lay out, then patch jump targets (an instruction index -> its pc; n+1: past the end)
    pcs, pc = [], 0
    for name, arg in items:
        pcs.append(pc)
        pc += 1 if name == "BAD" else len(ins(name, 0 if OPCODES[name][1] else None))
    pcs.append(pc)
    code = b""
    for name, arg in items:
        if name == "BAD":
            code += bytes([0xEE])
            continue
        if isinstance(arg, tuple):
            arg = pcs[arg[1]]
        elif name in ("RET", "RET0"):
            arg = None
        code += ins(name, arg)
    if rnd.random() < 0.1:
        code = code[:-rnd.randint(1, 3)]       # cut off: bad opcode at the end
    p = Program()
    p.name = "fuzz"
    p.version = 1
    p.strings = ["fuzz", "s", "hello", ""]
    p.globals = [rnd.choice([0, 5, -2147483648]), 1.5, Str(b"g"), Time(0)][:nglobals]
    p.externs = [("xi", 1, (INT,), INT), ("xf", 2, (FLOAT, FLOAT), FLOAT)]
    p.events = [("boot", 0, ()), ("enter", 0, ()), ("exit", 0, ()), ("go", 2, (INT, FLOAT)),
                ("out", 1, (INT,))]
    p.states = ["main"]
    p.timers = []
    starts = [rnd.choice(pcs[:-1]) for _ in range(3)]
    p.funcs = [("f%d" % i, a, a + rnd.randint(0, 2), starts[i]) for i, a in enumerate((0, 1, 2))]
    p.handlers = [(0, 3, 2, nlocals, 0)]
    p.code = code
    return p


def make_vm(prog, blocks, seen, budget):
    def xi(vm, a):
        seen.append(("xi", show(a[0])))
        return a[0] * 3 + 1
    def xf(vm, a):
        seen.append(("xf", show(a[0]), show(a[1])))
        return a[0] * a[1]
    rec = []
    vm = VM(prog, {"xi": xi, "xf": xf}, on_emit=lambda vm, n, a: seen.append(("emit", n, [show(x) for x in a])),
            on_fault=lambda vm, c, pc: seen.append(("fault", c, pc)), record=rec.append, budget=budget,
            stack_size=random.Random(len(prog.code)).choice([8, 16, 64]))
    vm.blocks = blocks
    vm.block_after = random.Random(len(prog.code) + 1).choice([0, 0, 1, 3])
    vm.rec_frames = rec
    return vm


def picture(vm):
    return (vm.pc, vm.cur_pc, vm.steps, vm.handler is None, [show(v) for v in vm.stack],
            [list(f) for f in vm.frames], [show(v) for v in vm.globals], vm.faults, b"".join(vm.rec_frames))


class Fuzz(unittest.TestCase):
    def test_random_bytecode(self):
        count = int(os.environ.get("FS_FUZZ", "600"))
        rnd = random.Random(int(os.environ.get("FS_FUZZ_SEED", "1234")))
        compiled = 0
        for case in range(count):
            prog = random_program(rnd)
            budget = rnd.choice([50, 300, 10000])
            seen_a, seen_b = [], []
            a, b = make_vm(prog, True, seen_a, budget), make_vm(prog, False, seen_b, budget)
            args = (rnd.choice(INTS), rnd.choice(FLOATS))
            for vm in (a, b):
                vm.start()
                vm.run()
                vm.post("go", *args)
                vm.post("go", args[0] + 1, 2.0)
            for _ in range(60):
                k = rnd.choice([1, 1, 2, 3, 5, 17, 100, 1000000])
                na, nb = a.run(k), b.run(k)
                where = "case %d, run(%d)" % (case, k)
                self.assertEqual(na, nb, where)
                self.assertEqual(picture(a), picture(b), where)
                self.assertEqual(seen_a, seen_b, where)
                if na == 0:
                    break
            compiled += sum(1 for v in a._blocks.values() if isinstance(v, tuple))
        self.assertGreater(compiled, count)          # blocks were actually used


class Programs(unittest.TestCase):
    def test_scenarios(self):
        """Every example and test program: the same trace and recording either way."""
        files = sorted(glob.glob(os.path.join(ROOT, "examples", "*.fs")) +
                       glob.glob(os.path.join(ROOT, "tests", "programs", "*.fs")))
        self.assertTrue(files)
        for path in files:
            with open(path) as f:
                _ast, prog, _ = compile_source(f.read())
            scen = os.path.splitext(path)[0] + ".scenario"
            text = ""
            if os.path.exists(scen):
                with open(scen) as f:
                    text = f.read()
            results = []
            saved = VM.blocks, VM.block_after
            for blocks in (True, False):
                VM.blocks, VM.block_after = blocks, 0       # blocks: compiled at once
                try:
                    lines, rec = [], []
                    run_scenario(prog, text, lines.append, record=rec.append)
                finally:
                    VM.blocks, VM.block_after = saved
                results.append((lines, b"".join(rec)))
            self.assertEqual(results[0], results[1], os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
