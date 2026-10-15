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

from tools.builtin._math_common import (
    MathParseError,
    _split_warnings,
    parse_params,
    safe_parse,
    serialize,
)

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
    variable: str = Field(
        "x",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "The ONE variable to solve for / differentiate / integrate / "
            "expand around. A single name, not a list: for a system, put "
            "the extra equations in `equations` and the solver takes its "
            "symbols from the system itself."
        ),
    )
    # `sympy.Symbol` accepts any string, so an unvalidated value
    # became a symbol that CANNOT occur in the parsed expression, and every
    # operation taking a variable then had a well-defined wrong answer:
    # `solve('x**2 - 4 = 0', variable='x, y')` returned `[]` and
    # `differentiate('x**3', variable='x, y')` returned `'0'`, neither
    # distinguishable in the result from a correct empty solution set or a
    # correct zero derivative. The schema is where its neighbours are
    # already validated (`order` ge=1, `precision` le=50), and the trigger
    # was the schema's own invitation: the sibling field `equations` IS a
    # list, and the solve-a-system path ignores `variable` entirely -- so
    # the one operation where a caller has a reason to name two variables
    # is the one where this field is unused (batch 143).
    #
    # `pattern` is a field the pinned `google-genai` `types.Schema`
    # carries, checked rather than assumed, so this does not widen the
    # GOOGLE-branch problem that is still open.
    symbols: Optional[list[str]] = Field(
        None,
        description=(
            "Other multi-character symbol names in the expression, e.g. "
            "['force', 'mass']. `variable` is always protected; any OTHER "
            "multi-character name is read as a product of single letters "
            "unless listed here ('mass' becomes a*m*s**2). Single letters "
            "and names containing an underscore are safe without this."
        ),
    )
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
    try:
        parsed = parse_params(SymbolicMathParams, params)
    except MathParseError as e:
        return {"error": str(e)}
    # `variable` PLUS whatever the caller declared, on EVERY parsed field.
    # Before batch 131 this tool passed `symbols=` on two of its six parses,
    # the one name a caller could protect survived and any other name in the
    # same expression was rewritten into a product of single letters --
    # `integrate('force*distance', variable='distance')` answered about
    # `c*e*f*o*r` without saying so.
    rewritten: set = set()
    syms = [parsed.variable] + [s for s in (parsed.symbols or [])
                                if s != parsed.variable]

    try:
        var = sympy.Symbol(parsed.variable)
        expr = safe_parse(parsed.expression, symbols=syms, rewritten=rewritten)
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
                lo = safe_parse(parsed.lower_bound, symbols=syms,
                                rewritten=rewritten)
                hi = safe_parse(parsed.upper_bound, symbols=syms,
                                rewritten=rewritten)
                result = sympy.integrate(expr, (var, lo, hi))
            else:
                result = sympy.integrate(expr, var)

        elif op == "limit":
            point = (safe_parse(parsed.point, symbols=syms, rewritten=rewritten)
                     if parsed.point is not None else 0)
            result = sympy.limit(expr, var, point, dir=parsed.direction)

        elif op == "series":
            point = (safe_parse(parsed.point, symbols=syms, rewritten=rewritten)
                     if parsed.point is not None else 0)
            result = expr.series(var, point, parsed.order).removeO()

        elif op == "solve":
            if parsed.equations:
                extra = [safe_parse(e, symbols=syms, rewritten=rewritten)
                         for e in parsed.equations]
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

    # H6, one batch late (batch 143). `serialize` is the SECOND place this function can fail
    # and it used to sit outside every try, so a correct answer merely too
    # large to print left `run()` as a bare ValueError and was reported as
    # a harness bug with a traceback. A tail handler rather than one try
    # round the whole body, which is the shape `discrete_math` -- the one
    # tool H6 reached -- already uses.
    try:
        out: dict = {"result": serialize(result, what=op), "operation": op}
    except MathParseError as e:
        return {"error": str(e)}
    if rewritten:
        out["warnings"] = _split_warnings(rewritten)
    return out
