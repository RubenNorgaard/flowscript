"""Straight-line bytecode compiled to Python functions: the Python VM's fast path.

A *block* is a run of simple instructions (pushes, loads and stores, int and float
arithmetic and comparisons, jumps, a few built-ins, CALL and RET) starting at some pc.
It is translated once into the source of a Python function that keeps the operand stack
in local variables and runs the whole run in one call; a back jump to its own start
loops inside the function.  XCALL, EMIT, GOTO, timers, logs, string operations and the
like end a block: the interpreter (vm.VM.execute) runs those one at a time.

Nothing may differ from running the same instructions one at a time with VM.execute:
values, faults (and the pc they are reported at), step counts.  So a block only does
what it can do *for sure* without a fault; anything else is a *bail*: it puts the stack
exactly as the interpreter would have it before instruction i and returns, and the
interpreter executes instruction i (and faults, or handles the unusual case).

    fn(vm, st, room) -> r
        st    vm.stack
        room  instructions the block may run (budget and max_steps left), >= its length
        r >= 0: done; r >> 32 is the next pc, r & 0xFFFFFFFF the instructions run
        r < 0:  bail; x = -1 - r: x >> 16 instructions ran, and the interpreter executes
                instruction x & 0xFFFF of the block (fn.pcs) next
"""

import math

from .intops import div as _idiv, mod as _imod, wrap as _wrap
from .values import Str, Time, fdiv as _fdiv

MAX_LEN = 200            # instructions per block (also keeps the source small)

_ARITH = {"ADD": "+", "SUB": "-", "MUL": "*"}
_CMP = {"EQ": "==", "NE": "!=", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}
_BITS = {"BAND": "&", "BOR": "|", "BXOR": "^"}
_BINOPS = set(_ARITH) | set(_CMP) | set(_BITS) | {"DIV", "MOD", "SHL", "SHR"}
_BUILTINS = {0, 24, 26, 27, 28, 29}     # float(int), abs(int), min/max (int, float)

# What is known about a value while compiling: int, float, a comparison (an int 0/1
# kept as a Python condition), other (str, time: not operated on here), or None.
I, F, C, O = "i", "f", "c", "o"


class _Stop(Exception):
    """The instruction cannot be compiled: the block ends before it."""


def _literal(e):
    """The int value of a literal entry, or None."""
    expr, typ = e
    if typ == I and expr.lstrip("-").isdigit():
        return int(expr)
    return None


class _Gen:
    def __init__(self, vm, start):
        self.vm = vm
        self.start = start
        self.lines = []
        self.ind = 2               # inside "def" and "while True"
        self.sym = []              # the stack above what the block found: (expr, type)
        self.below = 0             # values taken from the stack the block found
        self.maxbelow = 0
        self.maxrel = 0            # highest stack, relative to the one the block found
        self.maxlocal = -1         # highest LOADL/STOREL operand
        self.lcache = {}           # local slot -> its current value (expr, type)
        self.gcache = {}           # global index -> its current value
        self.ntemp = 0
        self.consts = {}           # name -> value (floats without a literal, Str, Time)
        self.pcs = []

    def save(self):
        return (len(self.lines), self.below, list(self.sym), self.maxbelow, self.maxrel,
                self.maxlocal, dict(self.lcache), dict(self.gcache), dict(self.consts))

    def restore(self, m):
        del self.lines[m[0]:]
        (self.below, self.sym, self.maxbelow, self.maxrel, self.maxlocal, self.lcache,
         self.gcache, self.consts) = m[1:]

    # -- helpers ------------------------------------------------------------------------
    def emit(self, line):
        self.lines.append("    " * self.ind + line)

    def temp(self):
        self.ntemp += 1
        return "t%d" % self.ntemp

    def const(self, v):
        name = "k%d" % len(self.consts)
        self.consts[name] = v
        return name

    def peek(self, n):
        """What is known about the types of the top n values (bottom first)."""
        out = [self.sym[-j][1] if j <= len(self.sym) else None for j in range(1, n + 1)]
        return out[::-1]

    def pop(self):
        if self.sym:
            return self.sym.pop()
        self.below += 1
        self.maxbelow = max(self.maxbelow, self.below)
        t = self.temp()
        self.emit("%s = st[L - %d]" % (t, self.below))
        return (t, None)

    def push(self, expr, typ):
        self.sym.append((expr, typ))
        self.maxrel = max(self.maxrel, len(self.sym) - self.below)

    @staticmethod
    def val(e):
        """The FlowScript value of an entry (a comparison becomes 0/1)."""
        expr, typ = e
        return "(1 if %s else 0)" % expr if typ == C else expr

    @staticmethod
    def cond(e):
        """An int entry as a Python condition (true when non-zero)."""
        expr, typ = e
        return expr if typ == C else "%s != 0" % expr

    def wrapped(self, t, expr):
        """t = intops.wrap(expr)"""
        self.emit("%s = %s" % (t, expr))
        self.emit("if %s > 2147483647 or %s < -2147483648: %s = _wrap(%s)" % (t, t, t, t))

    def flush(self, below, sym):
        """Code that makes the real stack what the interpreter would have."""
        if below:
            self.emit("del st[L - %d:]" % below)
        if len(sym) == 1:
            self.emit("st.append(%s)" % self.val(sym[0]))
        elif sym:
            self.emit("st.extend((%s,))" % ", ".join(self.val(e) for e in sym))

    def bail(self, state, i, cond=None):
        """if cond: the interpreter takes over at instruction i (state: the stack before it).
        Without cond: as the else branch of the if/elif just emitted."""
        self.emit("if %s:" % cond if cond else "else:")
        self.ind += 1
        if state:
            self.flush(*state)
        self.emit("return -1 - ((n + %d) << 16 | %d)" % (i, i))
        self.ind -= 1

    def leave(self, pc, count, cur):
        """Done: continue at pc after count instructions (cur: the last one's pc)."""
        self.flush(self.below, self.sym)
        self.emit("vm.cur_pc = %d" % cur)
        self.emit("return %d << 32 | n + %d" % (pc, count))

    def need(self, state, i, e, typ):
        """Make sure entry e has type typ (int or float); returns it known as such."""
        if e[1] == typ or (typ == I and e[1] == C):
            return e
        if e[1] is not None:
            raise _Stop()       # known to be another type: the interpreter faults or handles it
        self.bail(state, i, "type(%s) is not %s" % (e[0], "int" if typ == I else "float"))
        known = (e[0], typ)
        # the same value elsewhere (a copy on the stack, a variable) is now known too
        self.sym = [known if s[0] == e[0] else s for s in self.sym]
        for cache in (self.lcache, self.gcache):
            for k, v in cache.items():
                if v[0] == e[0]:
                    cache[k] = known
        return known

    # -- one instruction ---------------------------------------------------------------------
    def instr(self, i, pc, name, arg, npc):
        """Compile one instruction; False if it ends the block."""
        state = (self.below, list(self.sym))
        vm = self.vm
        if name == "NOP":
            pass
        elif name in ("PUSH", "PUSH8"):
            self.push(repr(arg), I)
        elif name == "PUSHF":
            literal = math.isfinite(arg) and float(repr(arg)) == arg
            self.push(repr(arg) if literal else self.const(arg), F)
        elif name == "PUSHT":
            self.push(self.const(Time(arg)), O)
        elif name == "PUSHS":
            if arg >= len(vm.prog.strings):
                raise _Stop()
            self.push(self.const(Str(vm.prog.strings[arg].encode("utf-8", "surrogateescape"))), O)
        elif name == "POP":
            if self.sym:
                self.sym.pop()
            else:
                self.below += 1
                self.maxbelow = max(self.maxbelow, self.below)
        elif name == "DUP":
            e = self.pop()
            self.push(*e)
            self.push(*e)
        elif name == "LOADG":
            if arg >= len(vm.globals):
                raise _Stop()
            if arg not in self.gcache:
                t = self.temp()
                self.emit("%s = g[%d]" % (t, arg))
                self.gcache[arg] = (t, None)
            self.push(*self.gcache[arg])
        elif name == "STOREG":
            if arg >= len(vm.globals):
                raise _Stop()
            e = self.pop()
            self.emit("g[%d] = %s" % (arg, self.val(e)))
            self.gcache[arg] = e
        elif name == "LOADL":
            self.maxlocal = max(self.maxlocal, arg)
            if arg not in self.lcache:
                t = self.temp()
                self.emit("%s = st[base + %d]" % (t, arg))
                self.lcache[arg] = (t, None)
            self.push(*self.lcache[arg])
        elif name == "STOREL":
            self.maxlocal = max(self.maxlocal, arg)
            e = self.pop()
            self.emit("st[base + %d] = %s" % (arg, self.val(e)))
            self.lcache[arg] = e
        elif name in ("NOT", "BOOL"):
            if self.peek(1)[0] in (F, O):
                raise _Stop()
            c = self.cond(self.need(state, i, self.pop(), I))
            self.push(("not (%s)" % c) if name == "NOT" else c, C)
        elif name in ("NEG", "BNOT"):
            self.unary(state, i, name)
        elif name in _BINOPS:
            self.binop(state, i, name)
        elif name == "BUILTIN":
            self.builtin(state, i, arg)
        elif name == "JMP":
            if arg == self.start and not self.below and not self.sym:
                # back to the start: loop here while there is room for another round
                self.emit("n += %d" % (i + 1))
                self.emit("if n + K <= room: continue")
                self.emit("vm.cur_pc = %d" % pc)
                self.emit("return %d << 32 | n" % arg)
            else:
                self.leave(arg, i + 1, pc)
            return False
        elif name in ("JZ", "JNZ"):
            if self.peek(1)[0] in (F, O):
                raise _Stop()
            c = self.cond(self.need(state, i, self.pop(), I))
            self.emit(("if not (%s):" if name == "JZ" else "if %s:") % c)
            self.ind += 1
            self.leave(arg, i + 1, pc)
            self.ind -= 1
        elif name == "CALL":
            if arg >= len(vm.prog.funcs):
                raise _Stop()
            _n, argc, nlocals, off = vm.prog.funcs[arg]
            zeros = max(0, nlocals - argc)
            self.flush(self.below, self.sym)        # the stack as the interpreter has it
            self.bail(None, i, "len(vm.frames) >= %d or len(st) < %d or len(st) + %d > %d"
                      % (vm.max_frames, argc, zeros, vm.stack_size))
            self.emit("vm.frames.append([%d, len(st) - %d])" % (npc, argc))
            if zeros:
                self.emit("st.extend(%r)" % ((0,) * zeros,))
            self.emit("vm.cur_pc = %d" % pc)
            self.emit("return %d << 32 | n + %d" % (off, i + 1))
            return False
        elif name in ("RET", "RET0"):
            v = self.val(self.pop()) if name == "RET" else "0"
            if name == "RET" and not v.isidentifier() and _literal((v, I)) is None:
                t = self.temp()
                self.emit("%s = %s" % (t, v))
                v = t
            # returning into a caller with a full stack: the push faults (1)
            self.bail(state, i, "len(vm.frames) > 1 and min(L + %d, vm.frames[-1][1]) >= %d"
                      % (len(self.sym) - self.below, vm.stack_size))
            self.flush(self.below, self.sym)
            self.emit("ret_pc, b = vm.frames.pop()")
            self.emit("del st[b:]")
            self.emit("vm.cur_pc = %d" % pc)
            self.emit("if not vm.frames:")
            self.emit("    vm.finish()")
            self.emit("    return %d << 32 | n + %d" % (npc, i + 1))
            self.emit("st.append(%s)" % v)
            self.emit("return ret_pc << 32 | n + %d" % (i + 1))
            return False
        else:
            raise _Stop()
        return True

    def unary(self, state, i, name):
        (ta,) = self.peek(1)
        if ta == O or (ta == F and name == "BNOT"):
            raise _Stop()
        e = self.pop()
        t = self.temp()
        if name == "BNOT":
            self.wrapped(t, "~" + self.val(self.need(state, i, e, I)))
            self.push(t, I)
        elif e[1] in (I, C):
            self.wrapped(t, "-" + self.val(e))
            self.push(t, I)
        elif e[1] == F:
            self.emit("%s = -%s" % (t, e[0]))
            self.push(t, F)
        else:
            a = e[0]
            self.emit("if type(%s) is int:" % a)
            self.ind += 1
            self.wrapped(t, "-" + a)
            self.ind -= 1
            self.emit("elif type(%s) is float: %s = -%s" % (a, t, a))
            self.bail(state, i)
            self.push(t, None)

    def binop(self, state, i, name):
        ta, tb = self.peek(2)
        ints = (I, C)
        int_only = name in _BITS or name in ("MOD", "SHL", "SHR")
        if O in (ta, tb) or (ta in ints and tb == F) or (ta == F and tb in ints) \
                or (int_only and F in (ta, tb)):
            raise _Stop()                 # the interpreter handles (or faults on) these
        b = self.pop()
        a = self.pop()
        want = I if (ta in ints or tb in ints or int_only) else F if F in (ta, tb) else None
        if want == I:
            self.int_op(state, i, name, self.need(state, i, a, I), self.need(state, i, b, I))
            return
        if want == F:
            self.float_op(name, self.need(state, i, a, F), self.need(state, i, b, F))
            return
        # both unknown: int or float, decided at run time
        A, B = a[0], b[0]
        if name in _CMP:
            self.bail(state, i, "type(%s) is not type(%s) or (type(%s) is not int and type(%s) is not float)"
                      % (A, B, A, A))
            self.push("%s %s %s" % (A, _CMP[name], B), C)
            return
        t = self.temp()
        if name == "DIV":
            self.emit("if type(%s) is int and type(%s) is int and %s != 0: %s = _idiv(%s, %s)"
                      % (A, B, B, t, A, B))
            self.emit("elif type(%s) is float and type(%s) is float: %s = _fdiv(%s, %s)" % (A, B, t, A, B))
        else:
            op = _ARITH[name]
            self.emit("if type(%s) is int and type(%s) is int:" % (A, B))
            self.ind += 1
            self.wrapped(t, "%s %s %s" % (A, op, B))
            self.ind -= 1
            self.emit("elif type(%s) is float and type(%s) is float: %s = %s %s %s" % (A, B, t, A, op, B))
        self.bail(state, i)
        self.push(t, None)

    def int_op(self, state, i, name, a, b):
        A, B = self.val(a), self.val(b)
        if name in _CMP:
            self.push("%s %s %s" % (A, _CMP[name], B), C)
            return
        t = self.temp()
        lit = _literal(b)
        if name in _ARITH:
            self.wrapped(t, "%s %s %s" % (A, _ARITH[name], B))
        elif name in _BITS:
            self.wrapped(t, "%s %s %s" % (A, _BITS[name], B))
        elif name == "SHL":
            self.wrapped(t, "(%s & 4294967295) << %s" % (A, lit & 31 if lit is not None else "(%s & 31)" % B))
        elif name == "SHR":
            self.wrapped(t, "%s >> %s" % (A, lit & 31 if lit is not None else "(%s & 31)" % B))
        elif lit is not None and lit > 0:
            # by a positive constant: truncating division without a call (intops.div/mod)
            if name == "DIV":
                self.wrapped(t, "%s // %d if %s >= 0 else -(-%s // %d)" % (A, lit, A, A, lit))
            else:
                self.wrapped(t, "%s %% %d if %s >= 0 else -(-%s %% %d)" % (A, lit, A, A, lit))
        else:
            if lit is None:
                self.bail(state, i, "%s == 0" % B)          # the interpreter faults (3)
            elif lit == 0:
                raise _Stop()
            self.emit("%s = %s(%s, %s)" % (t, "_idiv" if name == "DIV" else "_imod", A, B))
        self.push(t, I)

    def float_op(self, name, a, b):
        A, B = a[0], b[0]
        if name in _CMP:
            self.push("%s %s %s" % (A, _CMP[name], B), C)
            return
        t = self.temp()
        if name == "DIV":
            self.emit("%s = _fdiv(%s, %s)" % (t, A, B))
        else:
            self.emit("%s = %s %s %s" % (t, A, _ARITH[name], B))
        self.push(t, F)

    def builtin(self, state, i, idx):
        if idx not in _BUILTINS:
            raise _Stop()
        argc = 2 if idx >= 26 else 1
        want = F if idx in (28, 29) else I
        if any(t not in (None, want) and not (want == I and t == C) for t in self.peek(argc)):
            raise _Stop()
        args = [self.pop() for _ in range(argc)][::-1]
        args = [self.val(self.need(state, i, e, want)) for e in args]
        t = self.temp()
        if idx == 0:
            self.emit("%s = float(%s)" % (t, args[0]))
            self.push(t, F)
            return
        if idx == 24:
            self.wrapped(t, "abs(%s)" % args[0])
        else:                             # min, max: values.call_builtin's order
            a0, a1 = args
            self.emit("%s = %s if %s %s %s else %s" % (t, a1, a1, "<" if idx in (26, 28) else ">", a0, a0))
        self.push(t, want)


def compile_block(vm, pc):
    """The block starting at pc as (function, length), or None if it is not worth one."""
    g = _Gen(vm, pc)
    n = 0
    p = pc
    ended = False
    while n < MAX_LEN:
        try:
            _fast, name, arg, npc = vm._decode(p)
        except Exception:              # invalid here: the interpreter faults on it
            break
        mark = g.save()
        try:
            more = g.instr(n, p, name, arg, npc)
        except _Stop:
            g.restore(mark)
            break
        g.pcs.append(p)
        n += 1
        if not more:
            ended = True
            break
        last, p = p, npc
    if n < 2 and not (ended and n == 1 and g.pcs and name in ("CALL", "RET", "RET0")):
        return None
    if not ended:
        g.leave(p, n, last)
    head = ["def block(vm, st, room):", "    L = len(st)"]
    checks = ["L + %d > %d" % (g.maxrel, vm.stack_size)]
    if g.maxbelow:
        checks.append("L < %d" % g.maxbelow)
    head += ["    if %s:" % " or ".join(checks), "        return -1"]
    if g.maxlocal >= 0:
        head += ["    base = vm.frames[-1][1]",
                 "    if base + %d >= L - %d:" % (g.maxlocal, g.maxbelow),
                 "        return -1"]
    head += ["    g = vm.globals", "    n = 0", "    while True:"]
    src = "\n".join(head + g.lines) + "\n"
    ns = {"K": n, "_idiv": _idiv, "_imod": _imod, "_fdiv": _fdiv, "_wrap": _wrap}
    ns.update(g.consts)
    exec(compile(src, "<flowscript block at %d>" % pc, "exec"), ns)
    fn = ns["block"]
    fn.source = src
    fn.pcs = g.pcs
    return fn, n
