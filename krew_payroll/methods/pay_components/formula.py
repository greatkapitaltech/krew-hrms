"""
Safe formula parser for pay components (no eval).

Grammar: numbers, component codes (and CTC for earnings), + - * /, brackets,
min(a, b, ...) and max(a, b, ...). A reference to an unknown code evaluates
to 0; division by zero evaluates to 0.
"""

from decimal import Decimal, InvalidOperation

OPERATORS = "+-*/(),"


class FormulaError(ValueError):
    """Raised when a formula can't be parsed."""


def tokenize(source):
    tokens, i, text = [], 0, str(source or "")
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or ch == ".":
            j = i
            while j < len(text) and (text[j].isdigit() or text[j] == "."):
                j += 1
            try:
                tokens.append(("num", Decimal(text[i:j])))
            except InvalidOperation as exc:
                raise FormulaError(f'Bad number "{text[i:j]}"') from exc
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("id", text[i:j]))
            i = j
            continue
        if ch in OPERATORS:
            tokens.append(("op", ch))
            i += 1
            continue
        raise FormulaError(f'Unexpected character "{ch}"')
    return tokens


def parse(source):
    """Parse a formula into a small tuple-based AST."""
    tokens = tokenize(source)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        tok = peek()
        pos += 1
        return tok

    def expect(value):
        tok = take()
        if not tok or tok[1] != value:
            raise FormulaError(f'Expected "{value}"')

    def expr():
        node = term()
        while peek() and peek()[1] in ("+", "-"):
            node = ("bin", take()[1], node, term())
        return node

    def term():
        node = factor()
        while peek() and peek()[1] in ("*", "/"):
            node = ("bin", take()[1], node, factor())
        return node

    def factor():
        tok = take()
        if tok is None:
            raise FormulaError("Formula ends too early")
        kind, value = tok
        if kind == "num":
            return ("num", value)
        if kind == "op" and value == "-":
            return ("neg", factor())
        if kind == "op" and value == "(":
            node = expr()
            expect(")")
            return node
        if kind == "id":
            low = value.lower()
            if low in ("min", "max") and peek() and peek()[1] == "(":
                take()
                args = [expr()]
                while peek() and peek()[1] == ",":
                    take()
                    args.append(expr())
                expect(")")
                if len(args) < 2:
                    raise FormulaError(f"{low}() needs at least two values")
                return ("fn", low, args)
            return ("ref", value)
        raise FormulaError(f'Unexpected "{value}"')

    if not tokens:
        raise FormulaError("Formula is empty")
    ast = expr()
    if pos < len(tokens):
        raise FormulaError(f'Unexpected "{tokens[pos][1]}"')
    return ast


def evaluate(node, env):
    """Evaluate an AST against ``env`` (code -> Decimal)."""
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "ref":
        return Decimal(env.get(node[1], 0) or 0)
    if kind == "neg":
        return -evaluate(node[1], env)
    if kind == "fn":
        values = [evaluate(arg, env) for arg in node[2]]
        return min(values) if node[1] == "min" else max(values)
    left, right = evaluate(node[2], env), evaluate(node[3], env)
    op = node[1]
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    return left / right if right else Decimal(0)


def references(node, out=None):
    """Return the set of codes a formula refers to."""
    out = set() if out is None else out
    kind = node[0]
    if kind == "ref":
        out.add(node[1])
    elif kind == "neg":
        references(node[1], out)
    elif kind == "fn":
        for arg in node[2]:
            references(arg, out)
    elif kind == "bin":
        references(node[2], out)
        references(node[3], out)
    return out


def formula_references(source):
    """References of a formula, or an empty set when it doesn't parse."""
    try:
        return references(parse(source))
    except FormulaError:
        return set()
