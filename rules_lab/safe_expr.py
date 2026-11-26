"""受限因子公式解析与求值：只允许数字、白名单字段名、+ - * / 与括号。

明确禁止：eval / exec / 函数调用 / 属性访问 / 下标 / 字符串 / 名称查找 / 比较。
解析失败、未知字段、缺值、除零全部转成 FactorMissing（由解释器归为「待核验」），
绝不静默当 0。
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r'\s*(?:(\d+\.\d+|\d+|\.\d+)|([A-Za-z_][A-Za-z_0-9]*)|(.))')


class FormulaError(ValueError):
    pass


class FactorMissing(ValueError):
    def __init__(self, field, reason='MISSING'):
        super().__init__(field + ':' + reason)
        self.field = field
        self.reason = reason


class _Parser:
    def __init__(self, text, names):
        self.tokens = []
        pos = 0
        for m in _TOKEN.finditer(text):
            number, ident, other = m.groups()
            if number is not None:
                self.tokens.append(('NUM', float(number)))
            elif ident is not None:
                if ident not in names:
                    raise FormulaError('UNKNOWN_NAME:' + ident)
                self.tokens.append(('NAME', ident))
            elif other is not None:
                if other not in '+-*/()':
                    raise FormulaError('ILLEGAL_CHAR:' + other)
                self.tokens.append((other, other))
            pos = m.end()
        if text[pos:].strip():
            raise FormulaError('TRAILING_INPUT:' + text[pos:].strip())
        self.i = 0

    def peek(self):
        return self.tokens[self.i][0] if self.i < len(self.tokens) else 'EOF'

    def take(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self):
        value = self.expr()
        if self.peek() != 'EOF':
            raise FormulaError('UNEXPECTED_TOKEN:' + str(self.peek()))
        return value

    def expr(self):
        node = self.term()
        while self.peek() in ('+', '-'):
            op = self.take()[0]
            node = (op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek() in ('*', '/'):
            op = self.take()[0]
            node = (op, node, self.factor())
        return node

    def factor(self):
        kind, value = self.take()
        if kind == 'NUM':
            return ('num', value)
        if kind == 'NAME':
            return ('name', value)
        if kind == '(':
            node = self.expr()
            if self.peek() != ')':
                raise FormulaError('MISSING_RPAREN')
            self.take()
            return node
        if kind == '-':
            return ('neg', self.factor())
        if kind == '+':
            return self.factor()
        raise FormulaError('UNEXPECTED_FACTOR:' + str(kind))


def compile_formula(expr: str, allowed_names):
    if not isinstance(expr, str) or len(expr) > 256:
        raise FormulaError('EXPR_LENGTH')
    if any(word in expr for word in ('eval', 'exec', '__', 'import', 'lambda')):
        raise FormulaError('FORBIDDEN_WORD')
    return _Parser(expr, set(allowed_names)).parse()


def evaluate_formula(expr, env, allowed_names=None):
    names = set(allowed_names) if allowed_names is not None else set(env)
    tree = compile_formula(expr, names)

    def walk(node):
        kind = node[0]
        if kind == 'num':
            return node[1]
        if kind == 'name':
            name = node[1]
            value = env.get(name)
            if value is None:
                raise FactorMissing(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FactorMissing(name, 'NOT_NUMERIC')
            return float(value)
        if kind == 'neg':
            return -walk(node[1])
        a, b = walk(node[1]), walk(node[2])
        if kind == '+':
            return a + b
        if kind == '-':
            return a - b
        if kind == '*':
            return a * b
        if kind == '/':
            if abs(b) < 1e-12:
                raise FactorMissing('denominator', 'DIV_ZERO')
            return a / b
        raise FormulaError('BAD_NODE')

    return walk(tree)
