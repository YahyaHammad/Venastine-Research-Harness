"""
tools/builtin/symbolic_math.py

Algebra, calculus, trigonometry, and arithmetic -- all of these operate on
the same substrate (a single SymPy expression), so they share one tool
rather than being split further. Everything stays exact/symbolic (e.g.
Rational(1,3) not 0.333...) except the explicit "evaluate" operation,
which forces a numeric approximation.
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from pydantic import BaseModel, Field

from tools.builtin._math_common import safe_parse, serialize, MathParseError

_OPERATIONS = Literal[
    "evaluate", "simplify", "expand", "factor",
    "solve", "differentiate", "integrate", "limit", "series",
]


class SymbolicMathParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    expression: str = Field(
        ...,
        description=(
            "The math expression using standard notation, e.g. "
            "'x**2 + 3*x - 4', 'sin(x)*cos(x)', or 'x**2 - 4 = 0' for solve."
        ),
    )
    variable: str = Field("x", description="The variable to solve for / differentiate / integrate / expand around")
    equations: Optional[list[str]] = Field(
        None, description="For 'solve' with a system: additional equations alongside `expression`"
    )
    order: int = Field(1, ge=1, description="Order of differentiation, or number of terms for series expansion")
    lower_bound: Optional[str] = Field(None, description="Lower bound for a definite integral")
    upper_bound: Optional[str] = Field(None, description="Upper bound for a definite integral")
    point: Optional[str] = Field(None, description="Point to evaluate a limit or series expansion at, e.g. '0' or 'oo'")
    direction: Literal["+", "-", "+-"] = Field("+", description="Direction for limit evaluation")
    precision: int = Field(15, ge=1, le=50, description="Significant digits for the 'evaluate' operation")


TOOL_SCHEMA = {
    "name": "symbolic_math",
    "description": (
        "Perform exact symbolic algebra, calculus, and trigonometry: "
        "simplify, expand, factor, solve equations (single or systems), "
        "differentiate, integrate (definite or indefinite), evaluate "
        "limits, expand series, or numerically evaluate an expression. "
        "Results are exact (e.g. fractions, symbolic constants) unless "
        "'evaluate' is used."
    ),
    "input_schema": SymbolicMathParams.model_json_schema(),
}


def run(params: dict) -> dict:
    parsed = SymbolicMathParams(**params)

    try:
        var = sympy.Symbol(parsed.variable)
        expr = safe_parse(parsed.expression, symbols=[parsed.variable])
        op = parsed.operation

        if op == "evaluate":
            result = expr.evalf(parsed.precision)

        elif op == "simplify":
            result = sympy.simplify(expr)

        elif op == "expand":
            result = sympy.expand(expr)

        elif op == "factor":
            result = sympy.factor(expr)

        elif op == "differentiate":
            result = sympy.diff(expr, var, parsed.order)

        elif op == "integrate":
            if parsed.lower_bound is not None and parsed.upper_bound is not None:
                lo = safe_parse(parsed.lower_bound)
                hi = safe_parse(parsed.upper_bound)
                result = sympy.integrate(expr, (var, lo, hi))
            else:
                result = sympy.integrate(expr, var)

        elif op == "limit":
            point = safe_parse(parsed.point) if parsed.point is not None else 0
            result = sympy.limit(expr, var, point, dir=parsed.direction)

        elif op == "series":
            point = safe_parse(parsed.point) if parsed.point is not None else 0
            result = expr.series(var, point, parsed.order).removeO()

        elif op == "solve":
            if parsed.equations:
                extra = [safe_parse(e, symbols=[parsed.variable]) for e in parsed.equations]
                all_exprs = [expr] + extra
                symbols_found = sorted(
                    set().union(*[e.free_symbols for e in all_exprs]), key=str
                )
                result = sympy.solve(all_exprs, symbols_found)
            else:
                result = sympy.solve(expr, var)

        else:
            return {"error": f"Unknown operation: {op}"}

    except MathParseError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    return {"result": serialize(result), "operation": op}
