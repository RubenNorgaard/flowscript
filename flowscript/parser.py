"""FlowScript parser: tokens -> AST.

The AST is made of plain dicts (JSON ready). It *is* the visual model: the flowchart
is rendered from it and the formatter turns it back into source. Every node has
``id`` (unique per parse), ``type`` and ``line``; statements and declarations may
carry a ``note`` (comment text shown as an annotation).
"""

import sys

from .lexer import tokenize, FlowError, TYPE_KEYWORDS

BINARY_LEVELS = [
    ("or",),
    ("xor",),
    ("and",),
    None,  # not (prefix)
    ("==", "!=", "<", "<=", ">", ">="),
    ("|",),
    ("^",),
    ("&",),
    ("<<", ">>"),
    ("+", "-"),
    ("*", "/", "%"),
]
COMPARE_OPS = ("==", "!=", "<", "<=", ">", ">=")
MAX_NESTING = 100   # parenthesis depth; keeps parser/compiler recursion bounded

# expressions are parsed/compiled/printed recursively (~15 frames per nesting level)
if sys.getrecursionlimit() < 6000:
    sys.setrecursionlimit(6000)


class Parser:
    def __init__(self, src, legacy=False):
        self.legacy = legacy      # accept v0.1's any_state (only for flowscript.migrate)
        self.toks = tokenize(src)
        self.pos = 0
        self.next_id = 1
        self.depth = 0

    # -- helpers -----------------------------------------------------------
    def node(self, type_, tok, **fields):
        d = {"id": self.next_id, "type": type_, "line": tok.line if tok else 0}
        self.next_id += 1
        d.update(fields)
        return d

    @property
    def tok(self):
        return self.toks[self.pos]

    def peek(self, k=1):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def advance(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def is_kw(self, *words):
        return self.tok.kind == "KW" and self.tok.value in words

    def is_op(self, *ops):
        return self.tok.kind == "OP" and self.tok.value in ops

    def error(self, msg, tok=None):
        tok = tok or self.tok
        return FlowError(msg, tok.line, tok.col)

    def expect_kw(self, word):
        if not self.is_kw(word):
            raise self.error("expected '%s'" % word)
        return self.advance()

    def expect_op(self, op):
        if not self.is_op(op):
            raise self.error("expected '%s'" % op)
        return self.advance()

    def expect_name(self, what="name"):
        if self.tok.kind != "NAME":
            if self.tok.kind == "KW":
                raise self.error("'%s' is a keyword and cannot be used as %s" % (self.tok.value, what))
            raise self.error("expected %s" % what)
        return self.advance().value

    def end_of_line(self, node=None):
        """Consume an optional trailing comment and the newline."""
        if self.tok.kind == "COMMENT":
            text = self.advance().value
            if node is not None and text:
                node["note"] = (node["note"] + "\n" + text) if node.get("note") else text
        if self.tok.kind == "EOF":
            return
        if self.tok.kind != "NEWLINE":
            raise self.error("unexpected %s at end of statement" % self.describe(self.tok))
        self.advance()

    @staticmethod
    def describe(tok):
        if tok.kind == "INDENT":
            return "indentation"
        if tok.kind in ("OP", "KW"):
            return "'%s'" % tok.value
        if tok.kind == "NAME":
            return "name '%s'" % tok.value
        if tok.kind in ("NUM", "FLOAT"):
            return "number"
        return tok.kind.lower()

    def gather(self, out):
        """Skip blank lines and comments.

        Returns the note (comment lines directly above the next construct) or None.
        Comment groups followed by a blank line or by the end of a block are emitted
        into ``out`` as stand-alone comment nodes.
        """
        group = []
        group_tok = None
        prev_newline = True
        while True:
            t = self.tok
            if t.kind == "COMMENT":
                if not group:
                    group_tok = t
                group.append(t.value)
                self.advance()
                prev_newline = False
                continue
            if t.kind == "NEWLINE":
                self.advance()
                if prev_newline and group:
                    out.append(self.node("comment", group_tok, text="\n".join(group)))
                    group = []
                prev_newline = True
                continue
            break
        if group:
            if t.kind in ("EOF", "DEDENT"):
                out.append(self.node("comment", group_tok, text="\n".join(group)))
                return None
            return "\n".join(group)
        return None

    def colon(self, what):
        if not self.is_op(":"):
            raise self.error("expected ':' at the end of the %s line" % what)
        self.advance()

    def open_block(self, node, what):
        """``:`` + end of line + INDENT."""
        self.colon(what)
        self.end_of_line(node)
        if self.tok.kind != "INDENT":
            raise self.error("expected an indented block after the %s line" % what)
        self.advance()

    def skip_pass(self, out, note):
        """'pass' marks an empty block; a note above it is kept as a comment node."""
        t = self.advance()
        if note:
            out.append(self.node("comment", t, text=note))
        self.end_of_line()

    @staticmethod
    def attach(node, note):
        if note:
            node["note"] = note + ("\n" + node["note"] if node.get("note") else "")
        return node

    # -- program -----------------------------------------------------------
    def parse_program(self):
        prog = {"id": 0, "type": "program", "line": 1, "name": None, "version": 0, "items": []}
        items = prog["items"]
        pre = []
        note = self.gather(pre)
        if self.is_kw("flow"):
            prog["pre"] = pre
            self.advance()
            prog["name"] = self.expect_name("program name")
            if self.is_kw("version"):
                self.advance()
                if self.tok.kind != "NUM":
                    raise self.error("expected version number")
                prog["version"] = self.advance().value
            if note:
                prog["note"] = note
            self.end_of_line(prog)
            note = self.gather(items)
        else:
            items.extend(pre)
        while self.tok.kind != "EOF":
            item = self.parse_item()
            self.attach(item, note)
            items.append(item)
            note = self.gather(items)
        return prog

    def parse_params(self, typed=False):
        """'(a, b)' -> ([names], None) or, typed, '(int a, float b)' -> ([names], [types]).

        Returns (None, None) if there is no parameter list."""
        params, types = [], []
        if self.is_op("("):
            self.advance()
            if not self.is_op(")"):
                while True:
                    if typed:
                        if not self.is_type():
                            if self.tok.kind == "NAME":
                                raise self.error("give the parameter a type: 'int %s' (int, float, str or time)"
                                                 % self.tok.value)
                            raise self.error("expected a parameter type (int, float, str or time)")
                        types.append(self.advance().value)
                    params.append(self.expect_name("parameter name"))
                    if self.is_op(","):
                        self.advance()
                        continue
                    break
            self.expect_op(")")
            return params, (types if typed else None)
        return None, None

    def is_type(self):
        return self.tok.kind == "KW" and self.tok.value in TYPE_KEYWORDS

    def parse_returns(self, several=False):
        """Optional '-> type', or for functions '-> type, type, ...' (several results).

        Several result types are kept as one string, "float, int", as written."""
        if not self.is_op("->"):
            return None
        self.advance()
        types = []
        while True:
            if not self.is_type():
                raise self.error("expected a return type after '->' (int, float, str or time)")
            types.append(self.advance().value)
            if not self.is_op(","):
                break
            if not several:
                raise self.error("a host function (extern) returns at most one value")
            self.advance()
        return ", ".join(types)

    def var_hint(self):
        """'var x' was FlowScript v1: point to the typed declarations."""
        if self.tok.kind == "NAME" and self.tok.value == "var" and self.peek().kind == "NAME":
            raise self.error("declare variables with their type: 'int %s', 'float %s', 'str %s' or 'time %s'"
                             % ((self.peek().value,) * 4))

    def parse_decl(self, node_type):
        """'TYPE name [= expr]' (globals, state variables, locals)."""
        t = self.advance()
        name = self.expect_name("variable name")
        value = None
        if self.is_op("="):
            self.advance()
            value = self.parse_expr()
        n = self.node(node_type, t, name=name, vtype=t.value, value=value)
        self.end_of_line(n)
        return n

    def parse_item(self):
        t = self.tok
        if t.kind == "INDENT":
            raise self.error("unexpected indentation")
        self.check_end()
        self.var_hint()
        if t.kind != "KW":
            raise self.error("expected a declaration (extern, event, timer, const, int/float/str/time "
                             "variable, func, state, initial state, any_state)")
        kw = t.value
        if kw in TYPE_KEYWORDS:
            return self.parse_decl("var")
        if kw == "extern":
            self.advance()
            name = self.expect_name("extern name")
            params, types = self.parse_params(typed=True)
            if params is None:
                raise self.error("extern needs a parameter list, e.g. extern %s()" % name)
            n = self.node("extern", t, name=name, params=params, ptypes=types, returns=self.parse_returns())
            self.end_of_line(n)
            return n
        if kw == "event":
            self.advance()
            name = self.expect_name("event name")
            params, types = self.parse_params(typed=True)
            n = self.node("event", t, name=name, params=params or [], ptypes=types or [])
            self.end_of_line(n)
            return n
        if kw == "timer":
            self.advance()
            n = self.node("timer", t, name=self.expect_name("timer name"))
            self.end_of_line(n)
            return n
        if kw == "const":
            self.advance()
            name = self.expect_name()
            if not self.is_op("="):
                raise self.error("const needs a value")
            self.advance()
            n = self.node(kw, t, name=name, value=self.parse_expr())
            self.end_of_line(n)
            return n
        if kw == "func":
            self.advance()
            name = self.expect_name("function name")
            params, types = self.parse_params(typed=True)
            if params is None:
                raise self.error("func needs a parameter list, e.g. func %s()" % name)
            n = self.node("func", t, name=name, params=params, ptypes=types,
                          returns=self.parse_returns(several=True), body=[])
            self.open_block(n, "func")
            n["body"] = self.parse_block()
            return n
        if kw == "on":
            raise self.error("event handlers go inside a state: a program is always in exactly one "
                             "state, and only states handle events")
        if kw == "any_state" and self.legacy:
            self.advance()
            n = self.node("any_state", t, handlers=[])
            self.open_block(n, kw)
            while True:
                note = self.gather(n["handlers"])
                if self.tok.kind == "DEDENT":
                    self.advance()
                    return n
                if self.is_kw("pass"):
                    self.skip_pass(n["handlers"], note)
                    continue
                if not self.is_kw("on"):
                    raise self.error("only 'on' handlers are allowed inside any_state")
                n["handlers"].append(self.attach(self.parse_handler(), note))
        if kw == "any_state":
            raise self.error("'any_state' was removed: handle the event in each state that needs it "
                             "(share the logic with a func; 'on boot' goes in the initial state)")
        if kw == "initial":
            self.advance()
            if not self.is_kw("state"):
                raise self.error("'initial' marks a state: write 'initial state NAME:'")
            n = self.parse_item()
            n["initial"] = True
            return n
        if kw == "state":
            self.advance()
            n = self.node("state", t, name=self.expect_name("state name"), vars=[], handlers=[])
            self.open_block(n, kw)
            while True:
                loose = []
                note = self.gather(loose)
                # state variables come first; comments above them stay with them
                in_vars = self.is_type() and not n["handlers"]
                (n["vars"] if in_vars else n["handlers"]).extend(loose)
                if self.tok.kind == "DEDENT":
                    self.advance()
                    break
                if self.is_kw("pass"):
                    self.skip_pass(n["handlers"], note)
                    continue
                self.var_hint()
                if self.is_type():
                    if n["handlers"]:
                        raise self.error("state variables go at the top of the state, "
                                         "before its handlers")
                    n["vars"].append(self.attach(self.parse_decl("var"), note))
                    continue
                if not self.is_kw("on"):
                    raise self.error("only variables (int/float/str/time) and 'on' handlers are allowed inside a state")
                n["handlers"].append(self.attach(self.parse_handler(), note))
            return n
        raise self.error("unexpected '%s' at top level" % kw)

    def parse_handler(self):
        t = self.expect_kw("on")
        if self.tok.kind == "NAME" or self.is_kw("timer", "event"):
            event = self.advance().value
        else:
            raise self.error("expected event name")
        params, _ = self.parse_params()
        n = self.node("on", t, event=event, params=params, body=[])
        self.open_block(n, "on")
        n["body"] = self.parse_block()
        return n

    # -- statements --------------------------------------------------------
    def check_end(self):
        t = self.tok
        if t.kind == "NAME" and t.value == "goto" and self.peek().kind == "NAME":
            raise self.error("use 'goto_state %s' to change state" % self.peek().value)
        if t.kind == "NAME" and t.value == "emit" and self.peek().kind == "NAME":
            raise self.error("use 'raise_event %s' to raise an event" % self.peek().value)
        if t.kind == "NAME" and t.value == "end" and self.peek().kind in ("NEWLINE", "COMMENT", "EOF"):
            raise self.error("'end' is not used: a block ends where the indentation goes back")

    def parse_block(self):
        """Statements up to the DEDENT that closes the block (INDENT already consumed)."""
        body = []
        while True:
            note = self.gather(body)
            if self.tok.kind == "DEDENT":
                self.advance()
                return body
            if self.is_kw("pass"):
                self.skip_pass(body, note)
                continue
            body.append(self.attach(self.parse_stmt(), note))

    def parse_stmt(self):
        t = self.tok
        if t.kind == "INDENT":
            raise self.error("unexpected indentation")
        self.check_end()
        self.var_hint()
        if t.kind == "KW":
            kw = t.value
            if kw in TYPE_KEYWORDS and self.peek().kind == "NAME":
                return self.parse_decl("local")
            if kw in TYPE_KEYWORDS and self.peek().kind == "OP" and self.peek().value == "(":
                call = self.parse_primary()
                n = self.node("call", t, name=call["name"], args=call["args"])
                self.end_of_line(n)
                return n
            if kw == "if":
                self.advance()
                n = self.node("if", t, branches=[], else_body=None)
                cond = self.parse_expr()
                self.open_block(n, "if")
                n["branches"].append({"cond": cond, "body": self.parse_block()})
                while self.is_kw("elif"):
                    self.advance()
                    cond = self.parse_expr()
                    self.open_block(None, "elif")
                    n["branches"].append({"cond": cond, "body": self.parse_block()})
                if self.is_kw("else"):
                    self.advance()
                    self.open_block(None, "else")
                    n["else_body"] = self.parse_block()
                return n
            if kw == "while":
                self.advance()
                n = self.node("while", t, cond=None, body=[])
                n["cond"] = self.parse_expr()
                self.open_block(n, "while")
                n["body"] = self.parse_block()
                return n
            if kw == "goto_state":
                self.advance()
                n = self.node("goto", t, state=self.expect_name("state name"))
                self.end_of_line(n)
                return n
            if kw == "raise_event":
                self.advance()
                name = self.expect_name("event name")
                args = self.parse_args() if self.is_op("(") else []
                n = self.node("emit", t, event=name, args=args)
                self.end_of_line(n)
                return n
            if kw == "start":
                self.advance()
                name = self.expect_name("timer name")
                every = False
                if self.is_kw("every"):
                    self.advance()
                    every = True
                n = self.node("start", t, timer=name, every=every, ms=self.parse_expr())
                self.end_of_line(n)
                return n
            if kw == "stop":
                self.advance()
                n = self.node("stop", t, timer=self.expect_name("timer name"))
                self.end_of_line(n)
                return n
            if kw in ("elif", "else"):
                raise self.error("'%s' without a matching 'if' (check the indentation)" % kw)
            if kw == "return":
                self.advance()
                value, values = None, None
                if self.tok.kind not in ("NEWLINE", "COMMENT", "EOF"):
                    value = self.parse_expr()
                    if self.is_op(","):            # return a, b, ...: several results
                        values = [value]
                        while self.is_op(","):
                            self.advance()
                            values.append(self.parse_expr())
                        value = None
                n = self.node("return", t, value=value)
                if values:
                    n["values"] = values
                self.end_of_line(n)
                return n
            raise self.error("unexpected '%s'" % kw)
        if t.kind == "NAME":
            nxt = self.peek()
            if nxt.kind == "OP" and nxt.value == ",":   # a, b, c = f(...)
                targets = [self.advance().value]
                while self.is_op(","):
                    self.advance()
                    targets.append(self.expect_name("variable name"))
                self.expect_op("=")
                if self.tok.kind != "NAME" or not (self.peek().kind == "OP" and self.peek().value == "("):
                    raise self.error("several variables can only be assigned from a function call: "
                                     "%s = f(...)" % ", ".join(targets))
                call = self.parse_primary()
                n = self.node("massign", t, targets=targets, value=call)
                self.end_of_line(n)
                return n
            if nxt.kind == "OP" and nxt.value == "=":
                name = self.advance().value
                self.advance()
                n = self.node("assign", t, target=name, value=self.parse_expr())
                self.end_of_line(n)
                return n
            if nxt.kind == "OP" and nxt.value == "(":
                call = self.parse_primary()
                n = self.node("call", t, name=call["name"], args=call["args"])
                self.end_of_line(n)
                return n
            raise self.error("expected '=' or '(' after '%s'" % t.value, nxt)
        raise self.error("expected a statement")

    # -- expressions -------------------------------------------------------
    def parse_args(self):
        self.expect_op("(")
        args = []
        if not self.is_op(")"):
            while True:
                args.append(self.parse_expr())
                if self.is_op(","):
                    self.advance()
                    continue
                break
        self.expect_op(")")
        return args

    def parse_expr(self):
        return self.parse_level(0)

    def parse_level(self, level):
        if level >= len(BINARY_LEVELS):
            return self.parse_unary()
        ops = BINARY_LEVELS[level]
        if ops is None:  # prefix 'not'
            if self.is_kw("not"):
                t = self.advance()
                n = self.node("unary", t, op="not", operand=None)
                n["operand"] = self.parse_level(level)
                return n
            return self.parse_level(level + 1)
        left = self.parse_level(level + 1)
        while self.tok.kind in ("OP", "KW") and self.tok.value in ops:
            t = self.advance()
            right = self.parse_level(level + 1)
            n = self.node("binary", t, op=t.value, left=left, right=right)
            if t.value in COMPARE_OPS and self.tok.kind == "OP" and self.tok.value in COMPARE_OPS:
                raise self.error("comparisons cannot be chained; use 'and'")
            left = n
        return left

    def parse_unary(self):
        if self.is_op("-", "~"):
            t = self.advance()
            operand = self.parse_unary()
            if t.value == "-" and operand["type"] == "num" and operand["value"] >= 0:
                operand["value"] = -operand["value"]
                operand["text"] = "-" + operand.get("text", str(-operand["value"]))
                return operand
            n = self.node("unary", t, op=t.value, operand=operand)
            return n
        return self.parse_primary()

    def parse_primary(self):
        t = self.tok
        if t.kind in ("NUM", "FLOAT"):
            self.advance()
            return self.node("num", t, value=t.value, text=t.text)
        if t.kind == "KW" and t.value in TYPE_KEYWORDS and self.peek().kind == "OP" and self.peek().value == "(":
            self.advance()
            n = self.node("callx", t, name=t.value, args=[])
            n["args"] = self.parse_args()
            return n
        if t.kind == "STR":
            self.advance()
            return self.node("str", t, value=t.value)
        if t.kind == "KW" and t.value in ("true", "false"):
            self.advance()
            return self.node("bool", t, value=1 if t.value == "true" else 0)
        if t.kind == "NAME":
            self.advance()
            if self.is_op("("):
                n = self.node("callx", t, name=t.value, args=[])
                n["args"] = self.parse_args()
                return n
            return self.node("name", t, name=t.value)
        if self.is_op("("):
            self.advance()
            self.depth += 1
            if self.depth > MAX_NESTING:
                raise self.error("expression nested too deeply (max %d levels)" % MAX_NESTING)
            e = self.parse_expr()
            self.depth -= 1
            self.expect_op(")")
            return e
        raise self.error("expected an expression, got %s" % self.describe(t))


def parse(src, legacy=False):
    try:
        return Parser(src, legacy).parse_program()
    except RecursionError:
        raise FlowError("program is nested too deeply")


def parse_expression(text):
    p = Parser(text.strip())
    while p.tok.kind == "NEWLINE":
        p.advance()
    e = p.parse_expr()
    if p.tok.kind not in ("NEWLINE", "DEDENT", "EOF"):
        raise p.error("unexpected %s after expression" % p.describe(p.tok))
    return e
