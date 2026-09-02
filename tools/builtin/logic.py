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
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from sympy.logic.boolalg import simplify_logic, truth_table
from sympy.logic.inference import satisfiable
from pydantic import BaseModel, Field

from tools.builtin._math_common import safe_parse, MathParseError

_OPERATIONS = Literal["simplify", "equivalent", "satisfiable", "tautology", "truth_table"]


class LogicParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    expression: str = Field(
        ...,
        description="Boolean expression using variables and operators & (and), | (or), ~ (not), >> (implies)",
    )
    expression_b: Optional[str] = Field(None, description="Second expression, for the 'equivalent' operation")
    variables: Optional[list[str]] = Field(
        None, description="Explicit variable names/order -- mainly useful for truth_table"
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


def run(params: dict) -> dict:
    parsed = LogicParams(**params)

    try:
        var_names = parsed.variables or []
        expr = safe_parse(parsed.expression, symbols=var_names)
        op = parsed.operation

        if op == "simplify":
            result = str(simplify_logic(expr))

        elif op == "equivalent":
            if not parsed.expression_b:
                return {"error": "expression_b is required for 'equivalent'"}
            expr_b = safe_parse(parsed.expression_b, symbols=var_names)
            result = simplify_logic(expr) == simplify_logic(expr_b)

        elif op == "satisfiable":
            result = bool(satisfiable(expr))

        elif op == "tautology":
            result = not satisfiable(sympy.Not(expr))

        elif op == "truth_table":
            free_syms = sorted(expr.free_symbols, key=str)
            rows = list(truth_table(expr, free_syms))
            result = [
                {
                    "inputs": {str(s): bool(v) for s, v in zip(free_syms, row[0])},
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

    return {"result": result, "operation": op}
