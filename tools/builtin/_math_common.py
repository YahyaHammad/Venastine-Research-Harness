"""
tools/builtin/_math_common.py

Shared safe-parsing foundation for every math tool. Prefixed with an
underscore since this isn't itself a registered tool -- it's imported by
the six math tools that are.

SAFETY NOTE: SymPy's parser (sympify/parse_expr) tokenizes and then calls
Python's eval() on the result. If the namespace eval() runs against still
has real builtins available, a malicious or hallucinated expression
string could reach __import__, open, exec, etc. -- the same class of risk
the original calculator.py's `eval(expr, {"__builtins__": {}}, {})`
guarded against. This module applies the same fix: build a namespace that
has SymPy's public functions (sin, sqrt, Symbol, Matrix, ...) but an
explicitly emptied __builtins__.
"""

from __future__ import annotations

from typing import Any, Optional

import sympy
from sympy.parsing.sympy_parser import (
    parse_expr,
    standard_transformations,
    implicit_multiplication_application,
    convert_equals_signs,
)

# implicit_multiplication_application lets "2x" parse as "2*x".
# convert_equals_signs lets "x**2 - 4 = 0" parse into an Eq(...) object
# instead of raising a syntax error on the bare "=".
_TRANSFORMATIONS = standard_transformations + (
    implicit_multiplication_application,
    convert_equals_signs,
)

# SymPy's own public namespace (sin, cos, sqrt, pi, Symbol, Matrix, oo, ...)
# with __builtins__ explicitly neutered. Built once at import time.
_SAFE_GLOBALS: dict[str, Any] = {
    name: getattr(sympy, name) for name in dir(sympy) if not name.startswith("_")
}
_SAFE_GLOBALS["__builtins__"] = {}


class MathParseError(ValueError):
    """Raised when an expression string can't be safely parsed."""


def safe_parse(expr_str: str, symbols: Optional[list[str]] = None):
    """
    Parses a math expression string into a SymPy object without exposing
    Python builtins. `symbols` is optional -- SymPy's standard
    transformations already auto-convert undefined names into Symbols,
    but passing them explicitly keeps variable identity consistent across
    multiple calls (e.g. the same 'x' in an expression and a separate
    bound for a definite integral).
    """
    local_dict = {name: sympy.Symbol(name) for name in (symbols or [])}
    try:
        return parse_expr(
            expr_str,
            local_dict=local_dict,
            global_dict=_SAFE_GLOBALS,
            transformations=_TRANSFORMATIONS,
        )
    except Exception as e:
        raise MathParseError(f"Could not parse expression '{expr_str}': {e}") from e


def serialize(value: Any) -> Any:
    """
    Generic SymPy-object -> JSON-safe converter, used across all six math
    tools. Matrices become nested lists of strings, dicts/lists recurse,
    everything else falls back to str().
    """
    if isinstance(value, sympy.Matrix):
        return [[str(cell) for cell in row] for row in value.tolist()]
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    if isinstance(value, bool):
        return value
    return str(value)
