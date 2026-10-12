"""
ZEUS Calculator Plugin
A real, working example plugin demonstrating the expected shape:
PLUGIN_INFO metadata + a register(registry) function that adds tools.
"""

import ast
import operator

from core.security_manager import RiskLevel

PLUGIN_INFO = {
    "name": "calculator",
    "version": "1.0.0",
    "author": "ZEUS",
    "permissions": [],  # no filesystem/network access needed
}

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")


def calculate(expression: str):
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree.body)
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"error": f"Could not evaluate '{expression}': {e}"}


def register(registry):
    registry.register(
        "calculate", "Evaluate a basic arithmetic expression safely",
        RiskLevel.SAFE, params={"expression": "math expression, e.g. 25 * 48"},
    )(calculate)
