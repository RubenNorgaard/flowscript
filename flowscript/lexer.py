"""FlowScript lexer: source text -> list of tokens."""

KEYWORDS = {
    "flow", "version", "extern", "event", "timer", "const", "var", "func", "on",
    "state", "initial", "any_state", "pass", "if", "elif", "else", "while", "goto_state", "raise_event", "start", "every",
    "stop", "return", "and", "or", "xor", "not", "true", "false",
}

TIME_UNITS = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}

# longest first
OPERATORS = ["<<", ">>", "==", "!=", "<=", ">=", "+", "-", "*", "/", "%", "&", "|",
             "^", "~", "<", ">", "=", "(", ")", ",", ":"]


class FlowError(Exception):
    def __init__(self, msg, line=0, col=0):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col

    def __str__(self):
        if self.line:
            return "line %d:%d: %s" % (self.line, self.col, self.msg)
        return self.msg

    def to_dict(self):
        return {"msg": self.msg, "line": self.line, "col": self.col}


class Token:
    __slots__ = ("kind", "value", "line", "col", "text")

    def __init__(self, kind, value, line, col, text=None):
        self.kind = kind      # NAME KW NUM STR OP NEWLINE COMMENT INDENT DEDENT EOF
        self.value = value
        self.line = line
        self.col = col
        self.text = text      # original spelling (numbers)

    def __repr__(self):
        return "Token(%s,%r,%d:%d)" % (self.kind, self.value, self.line, self.col)


def tokenize(src):
    """Tokens including INDENT/DEDENT (blocks are delimited by indentation)."""
    return _layout(*_raw_tokens(src))


def _raw_tokens(src):
    toks = []
    indents = {}      # line -> indentation width of that line
    i = 0
    line = 1
    col = 1
    n = len(src)

    def add(kind, value, l, c, text=None):
        toks.append(Token(kind, value, l, c, text))

    while i < n:
        ch = src[i]
        if col == 1:
            j = i
            while j < n and src[j] in " \t\r":
                j += 1
            if j < n and src[j] != "\n":
                if "\t" in src[i:j]:
                    raise FlowError("tab used for indentation; indent with spaces", line, 1)
                indents[line] = j - i
        if ch == "\n":
            add("NEWLINE", None, line, col)
            i += 1
            line += 1
            col = 1
            continue
        if ch in " \t\r":
            i += 1
            col += 1
            continue
        if ch == "#":
            j = src.find("\n", i)
            if j < 0:
                j = n
            text = src[i + 1:j]
            if text.startswith(" "):
                text = text[1:]
            add("COMMENT", text.rstrip(), line, col)
            col += j - i
            i = j
            continue
        if ch.isdigit():
            j = i
            if src.startswith(("0x", "0X"), i):
                j = i + 2
                while j < n and (src[j] in "0123456789abcdefABCDEF_"):
                    j += 1
                base = 16
                digits = src[i + 2:j]
            elif src.startswith(("0b", "0B"), i):
                j = i + 2
                while j < n and src[j] in "01_":
                    j += 1
                base = 2
                digits = src[i + 2:j]
            else:
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
                base = 10
                digits = src[i:j]
            digits = digits.replace("_", "")
            if not digits:
                raise FlowError("malformed number", line, col)
            value = int(digits, base)
            # time unit suffix
            k = j
            while k < n and (src[k].isalpha() or src[k] == "_" or src[k].isdigit()):
                k += 1
            unit = src[j:k]
            if unit:
                if base != 10 or unit not in TIME_UNITS:
                    raise FlowError("bad number suffix '%s' (allowed: ms, s, min, h)" % unit, line, col)
                value *= TIME_UNITS[unit]
            text = src[i:k]
            add("NUM", value, line, col, text)
            col += k - i
            i = k
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            add("KW" if word in KEYWORDS else "NAME", word, line, col)
            col += j - i
            i = j
            continue
        if ch == '"':
            j = i + 1
            out = []
            while True:
                if j >= n or src[j] == "\n":
                    raise FlowError("unterminated string", line, col)
                c = src[j]
                if c == '"':
                    break
                if c == "\\":
                    j += 1
                    esc = src[j] if j < n else ""
                    out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                else:
                    out.append(c)
                j += 1
            add("STR", "".join(out), line, col)
            col += j + 1 - i
            i = j + 1
            continue
        for op in OPERATORS:
            if src.startswith(op, i):
                add("OP", op, line, col)
                i += len(op)
                col += len(op)
                break
        else:
            raise FlowError("unexpected character %r" % ch, line, col)
    add("NEWLINE", None, line, col)
    return toks, indents, line, col


def _layout(toks, indents, last_line, last_col):
    """Insert INDENT/DEDENT tokens, Python style.

    Blank and comment-only lines do not open or close blocks. A comment group directly
    above a code line is placed after that line's INDENT/DEDENT tokens (it becomes the
    line's note). Other comment groups stay in the innermost block that their own
    indentation allows.
    """
    # split into physical lines (each ends with NEWLINE)
    lines = []
    cur = []
    for t in toks:
        cur.append(t)
        if t.kind == "NEWLINE":
            lines.append(cur)
            cur = []
    out = []
    stack = [0]
    pending = []

    def dedent_to(width, tok):
        while stack[-1] > width:
            stack.pop()
            out.append(Token("DEDENT", None, tok.line, 1))

    def flush_standalone(group, floor):
        for ln in group:
            if ln[0].kind == "COMMENT":
                dedent_to(max(indents.get(ln[0].line, 0), floor), ln[0])
            out.extend(ln)

    for ln in lines:
        first = ln[0]
        if first.kind in ("NEWLINE", "COMMENT"):
            pending.append(ln)
            continue
        width = indents.get(first.line, 0)
        # the comment group directly above this line (no blank line between) is its note
        k = len(pending)
        while k > 0 and pending[k - 1][0].kind == "COMMENT":
            k -= 1
        standalone, note = pending[:k], pending[k:]
        continuation = first.kind == "KW" and first.value in ("elif", "else")
        if continuation:
            standalone, note = pending, []
        floor = width
        if continuation:
            deeper = [w for w in stack if w > width]
            floor = min(deeper) if deeper else width
        pending = []
        if width > stack[-1]:
            # blank lines / comments between a header and its block belong to the block
            stack.append(width)
            out.append(Token("INDENT", None, first.line, 1))
            flush_standalone(standalone, width)
        else:
            flush_standalone(standalone, floor)
        if width < stack[-1]:
            dedent_to(width, first)
            if stack[-1] != width:
                raise FlowError("indentation does not match any outer block", first.line, width + 1)
        for nl in note:
            out.extend(nl)
        out.extend(ln)
    end = Token("EOF", None, last_line, last_col)
    flush_standalone(pending, 0)
    dedent_to(0, end)
    out.append(end)
    return out
