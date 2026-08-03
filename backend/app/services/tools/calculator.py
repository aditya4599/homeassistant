"""Safe arithmetic: parsed with ``ast`` and evaluated from an explicit allowlist.

No ``eval``, no names except a few math functions/constants, bounded exponents and
result size, so a model (or a user) can't use it to run code or hang the server.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, schema

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}  # fmt: skip
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "floor": math.floor, "ceil": math.ceil,
}  # fmt: skip
_CONSTS = {"pi": math.pi, "e": math.e}
_MAX_EXPONENT = 1000
_MAX_ABS = 1e100
_MAX_LEN = 200

_WORDS = [
    (r"\bmultiplied by\b|\btimes\b|\bx\b|×", "*"),
    (r"\bdivided by\b|÷", "/"),
    (r"\bplus\b", "+"),
    (r"\bminus\b", "-"),
    (r"\bto the power of\b|\^", "**"),
    (r"\bsquared\b", "**2"),
    (r"\bcubed\b", "**3"),
    (r"\bsquare root of\b", "sqrt"),
]


class CalculationError(ValueError):
    pass


def normalize_expression(text: str) -> str:
    expr = text.strip().lower().rstrip("?.! ")
    expr = re.sub(r"^(what(?:'s| is)|calculate|compute|evaluate)\s+", "", expr)
    expr = expr.replace(",", "")
    # "15% of 840" -> (15/100*840); "15% " alone -> (15/100)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", expr)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", r"(\1/100)", expr)
    for pattern, repl in _WORDS:
        expr = re.sub(pattern, repl, expr)
    expr = re.sub(r"\bsqrt\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", expr)
    return expr


def _eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise CalculationError("exponent too large")
        try:
            value = _BIN[type(node.op)](left, right)
        except ZeroDivisionError:
            raise CalculationError("division by zero") from None
        return _check(value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
        and not node.keywords and 1 <= len(node.args) <= 2
    ):  # fmt: skip
        try:
            return _check(_FUNCS[node.func.id](*(_eval(a) for a in node.args)))
        except (ValueError, OverflowError) as exc:
            raise CalculationError(str(exc)) from None
    raise CalculationError("unsupported expression")


def _check(value: Any) -> float | int:
    if isinstance(value, complex) or (isinstance(value, float) and not math.isfinite(value)):
        raise CalculationError("result is not a real number")
    if abs(value) > _MAX_ABS:
        raise CalculationError("result too large")
    return value


def calculate(text: str) -> float | int:
    expr = normalize_expression(text)
    if not expr or len(expr) > _MAX_LEN:
        raise CalculationError("expression is empty or too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise CalculationError(f"could not parse {expr!r}") from None
    return _eval(tree)


def format_number(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.6g}" if abs(value) >= 1e-4 else f"{value:.6g}"


def calculator_tools() -> list[Tool]:
    async def calc(args: dict[str, Any]) -> ToolResult:
        expression = arg_str(args, "expression")
        if not expression:
            raise ValueError("expression is required")
        try:
            value = calculate(expression)
        except CalculationError as exc:
            return ToolResult(
                ok=False, say="I couldn't calculate that.", error_code="calculation_error",
                data={"detail": str(exc)},
            )  # fmt: skip
        spoken = format_number(value)
        return ToolResult(ok=True, say=f"That's {spoken}.", data={"result": value, "spoken": spoken})

    return [
        Tool(ToolDefinition(
            "calculate",
            "Exact arithmetic. ALWAYS use for any calculation instead of doing math yourself. "
            "Supports + - * / ** %, 'X% of Y', sqrt, parentheses.",
            schema({"expression": {"type": "string",
                                   "description": "e.g. '27 * 43' or '15% of 840'"}}, ["expression"]),
        ), calc),
    ]  # fmt: skip
