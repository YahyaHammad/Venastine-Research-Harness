"""
tools/builtin/logic.py

Propositional logic: simplification, equivalence checking, satisfiability,
tautology checking, and truth tables.

SCOPE NOTE, worth being honest about: this mechanically verifies
PROPOSITIONAL claims (is this specific expression a tautology, are these
two expressions equivalent, is this satisfiable). It is NOT a full
first-order proof assistant -- it can't verify a multi-step natural-
deduction proof or reason over quantifiers. What it CAN do is give the
model a deterministic ground-truth check for a specific logical claim,
which is still real uplift over the model reasoning about it unaided.

AND THAT IS WHY THE DOMAIN IS CHECKED (batch 143). `safe_parse` admits
the whole 230-name allowlist, so `expression` need not be Boolean, and no
operation used to look. SymPy's `truth_table` substitutes 0/1 and coerces
the arithmetic value through `bool()`, so `x - 1` was returned as a
confident NOT table and `x + y` as the truth table of OR -- which has a
plausible caller, because in GF(2) and in coding theory `x + y` IS XOR.
`bool()` of an arithmetic value is not a ground-truth check for a logical
claim; it is a coincidence that agrees with OR for `+` and with AND for
`*` and diverges elsewhere. A tool whose stated value is that it is
mechanical has to refuse the inputs it cannot be mechanical about, so
`_require_boolean` is the paragraph above enforced rather than asserted.
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from pydantic import BaseModel, Field
from sympy.logic.boolalg import Boolean, simplify_logic, truth_table
from sympy.logic.inference import satisfiable

from tools.builtin._math_common import (
    MathParseError,
    _split_warnings,
    parse_params,
    safe_parse,
)

_OPERATIONS = Literal["simplify", "equivalent", "satisfiable", "tautology", "truth_table"]

# THE OUTPUT BOUND (batch 143), on the INPUT rather than on the rendered
# output, because this is the one math operation whose size is predictable
# before anything is computed: `truth_table` materialises 2**n rows into a
# list where n comes from a model-supplied string. H5's move -- refuse at
# the boundary rather than pay a full budget to learn the answer cannot be
# returned -- and it does NOT touch H4 ("no maximum on any math
# parameter"), because the symbol count is derived from the expression and
# is not a parameter anyone can cap.
#
# Measured on the real `logic.run`, with `config.MAX_READ_CHARS` (50,000)
# as the ceiling the other five tools get in `serialize`:
#
#     columns   rows    chars returned   secs
#        7       128         15,207      0.01
#        8       256         33,575      0.01   <- the last one under
#        9       512         73,511      0.03
#       10     1,024        159,783      0.06
#       14    16,384      3,440,679      1.36   <- returned to the model
#       15    32,768      7,323,687      3.03      before this bound
#
# So 8 is where a character cap would have landed anyway; stating it in
# columns is what lets the refusal name the number the caller controls.
# Nothing in the suite's own battery exceeds 3 columns.
_MAX_TRUTH_TABLE_SYMBOLS = 8


class LogicParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    expression: str = Field(
        ...,
        description=(
            "Boolean expression using variables and operators & (and), "
            "| (or), ~ (not), >> (implies), Xor, Equivalent -- or a "
            "comparison such as Eq(x, 5) or x > 0. It must be a "
            "PROPOSITION: an arithmetic expression like 'x + y' has no "
            "truth value and is refused."
        ),
    )
    expression_b: Optional[str] = Field(None, description="Second expression, for the 'equivalent' operation")
    variables: Optional[list[str]] = Field(
        None,
        description=(
            "Variable names. A name listed here is kept WHOLE -- that is "
            "the load-bearing half: any multi-character name NOT listed is "
            "read as a product of single letters, so 'rain' alone becomes "
            "a*i*n*r (a 16-row table over a, i, n, r) and 'rain & snow' is "
            "a parse error. It also sets the column set and order for "
            "truth_table, and a listed name that does not occur in the "
            "expression becomes a constant column."
        ),
    )


TOOL_SCHEMA = {
    "name": "logic",
    "description": (
        "Mechanically check propositional logic claims: simplify a boolean "
        "expression, check whether two expressions are logically "
        "equivalent, check satisfiability, check whether an expression is "
        "a tautology (always true), or generate a full truth table (inputs and output as true/false booleans)."
    ),
    "input_schema": LogicParams.model_json_schema(),
}


def _require_boolean(expr, field: str, rewritten: set) -> None:
    """Refuse an expression that is not a proposition. See the module docstring.

    ONE `isinstance`, not a union of four types, and that was measured
    rather than assumed: `Symbol`, `BooleanTrue`, `BooleanFalse` and every
    `Relational` are already `Boolean` subclasses, so the single check
    accepts `a & b`, `a >> b`, `~a`, `Xor(a, b)`, `ITE(a, b, c)`,
    `Eq(x, 5)`, `x > 0`, `a`, `true`, `1 = 1` and refuses `x - 1`,
    `x + y`, `x**2`, `pi`, `2`, `Matrix([[1]])`. One name is also CLOSED
    over whatever Boolean type a future SymPy adds, which an enumeration
    of four would not be.

    `a & 1` and `a | 0` never reach here -- SymPy's own `&` raises on an
    Integer operand, so an arithmetic constant inside a formula was
    already refused loudly (batch 143).
    """
    if isinstance(expr, Boolean):
        return
    message = (
        "%s parsed as %s, which has no truth value. This tool checks "
        "PROPOSITIONAL claims: combine variables with & (and), | (or), "
        "~ (not), >> (implies), Xor or Equivalent, or use a comparison "
        "such as Eq(x, 5) or x > 0. Substituting 0 and 1 into an "
        "arithmetic expression answers a different question -- 'x + y' "
        "would come back as the truth table of OR."
        % (field, type(expr).__name__))
    # The commonest way to land here is not an arithmetic expression at
    # all: `truth_table expression="rain"` parses as `a*i*n*r`, a Mul, so
    # the domain refusal fires on a perfectly good propositional name. The
    # cause the caller can act on is the SPLIT, so it is appended rather
    # than left for them to infer from "Mul" (batch 143).
    if rewritten:
        message += " " + " ".join(_split_warnings(rewritten, "variables"))
    raise MathParseError(message)


def _truth_table_columns(expr, declared: list[str]) -> list:
    """The column set and order for a truth table.

    The declared names FIRST, in the order given, then every free symbol
    the caller did not declare, sorted. The second half is not optional and
    is why this is a function: handed a list that does not cover every free
    symbol, SymPy's `truth_table` leaves the rest UNSUBSTITUTED and returns
    the symbol itself as the output -- measured, `truth_table(a & b, [a])`
    gives `([1], b)`, and `bool(Symbol('b'))` is then drawn as `true`. So
    "the declared names" alone would have replaced an ignored parameter
    with a wrong table.

    A declared name absent from the expression becomes a constant column,
    which is what a caller asking for an explicit order wants (batch 143).
    """
    free = {str(s): s for s in expr.free_symbols}
    columns, seen = [], set()
    for name in declared:
        if name not in seen:
            seen.add(name)
            columns.append(free[name] if name in free else sympy.Symbol(name))
    for name in sorted(free):
        if name not in seen:
            seen.add(name)
            columns.append(free[name])
    return columns


def run(params: dict) -> dict:
    try:
        parsed = parse_params(LogicParams, params)
    except MathParseError as e:
        return {"error": str(e)}

    # The fourth parsing tool reports a split name too. Batch 131 added
    # this to the three tools that could not reach `symbols=` at all and
    # left this one out, because `variables` already reached it -- but a
    # caller who does not pass it still gets an answer about a product of
    # single letters, and in the FUNCTIONAL spellings that answer is
    # Boolean and therefore silent: `Xor(rain, b)`, `Not(rain)`,
    # `Eq(rain, 5)` and `Implies(rain, b)` all parse, all pass the domain
    # check, and `truth_table` then draws 16 rows over a, i, n, r as a
    # success. Only the infix spellings (`rain & b`) fail loudly, and they
    # fail naming SymPy's operand types (batch 143).
    rewritten: set = set()

    try:
        var_names = parsed.variables or []
        expr = safe_parse(parsed.expression, symbols=var_names,
                          rewritten=rewritten)
        _require_boolean(expr, "expression", rewritten)
        op = parsed.operation

        if op == "simplify":
            result = str(simplify_logic(expr))

        elif op == "equivalent":
            if not parsed.expression_b:
                return {"error": "expression_b is required for 'equivalent'"}
            expr_b = safe_parse(parsed.expression_b, symbols=var_names,
                                rewritten=rewritten)
            _require_boolean(expr_b, "expression_b", rewritten)
            result = simplify_logic(expr) == simplify_logic(expr_b)

        elif op == "satisfiable":
            result = bool(satisfiable(expr))

        elif op == "tautology":
            result = not satisfiable(sympy.Not(expr))

        elif op == "truth_table":
            # `variables` decides the columns when it is given, which is
            # the half of its description that used to be inert: this list
            # was `sorted(expr.free_symbols, key=str)` regardless, so
            # neither the requested SET nor the requested ORDER had any
            # effect, while the field was silently load-bearing for the
            # parse (batch 143).
            columns = _truth_table_columns(expr, var_names)
            if len(columns) > _MAX_TRUTH_TABLE_SYMBOLS:
                return {"error":
                        "truth_table would need %d columns, which is 2**%d "
                        "rows -- too large to return. The ceiling is %d "
                        "columns (%d rows). Ask about fewer variables, or "
                        "use 'satisfiable' or 'tautology', which answer "
                        "whether a row exists without enumerating any."
                        % (len(columns), len(columns),
                           _MAX_TRUTH_TABLE_SYMBOLS,
                           2 ** _MAX_TRUTH_TABLE_SYMBOLS)}
            rows = list(truth_table(expr, columns))
            result = [
                {
                    "inputs": {str(s): bool(v) for s, v in zip(columns, row[0])},
                    "output": bool(row[1]),
                }
                for row in rows
            ]

        else:
            return {"error": f"Unknown operation: {op}"}

    except MathParseError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    out: dict = {"result": result, "operation": op}
    if rewritten:
        out["warnings"] = _split_warnings(rewritten, "variables")
    return out
