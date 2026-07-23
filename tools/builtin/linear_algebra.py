"""
tools/builtin/linear_algebra.py

Matrices, vectors, and low-order tensors (i.e. rank <= 2 -- this doesn't
attempt full tensor-index calculus, just matrix/vector operations, which
covers the overwhelming majority of research-applicable linear algebra).
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from pydantic import BaseModel, Field

from tools.builtin._math_common import safe_parse, serialize, MathParseError

_OPERATIONS = Literal[
    "add", "subtract", "multiply", "transpose", "determinant",
    "inverse", "eigenvalues", "eigenvectors", "rank",
    "solve_linear_system", "dot_product", "cross_product", "norm",
]


class LinearAlgebraParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    matrix_a: Optional[list[list[str]]] = Field(
        None, description="First matrix, as rows of string entries (numbers or symbolic expressions)"
    )
    matrix_b: Optional[list[list[str]]] = Field(
        None, description="Second matrix -- needed for add/subtract/multiply/solve_linear_system"
    )
    vector_a: Optional[list[str]] = Field(None, description="First vector -- needed for dot_product/cross_product/norm")
    vector_b: Optional[list[str]] = Field(None, description="Second vector -- needed for dot_product/cross_product")
    scalar: Optional[str] = Field(None, description="Scalar value, for scalar multiplication of matrix_a")


TOOL_SCHEMA = {
    "name": "linear_algebra",
    "description": (
        "Perform matrix and vector operations: arithmetic, transpose, "
        "determinant, inverse, eigenvalues/eigenvectors, rank, solving "
        "linear systems (Ax=b), dot product, cross product, and norm. "
        "Entries can be exact numbers or symbolic expressions."
    ),
    "input_schema": LinearAlgebraParams.model_json_schema(),
}


def _to_matrix(rows: list[list[str]]) -> sympy.Matrix:
    return sympy.Matrix([[safe_parse(cell) for cell in row] for row in rows])


def _to_vector(values: list[str]) -> sympy.Matrix:
    return sympy.Matrix([safe_parse(v) for v in values])


def run(params: dict) -> dict:
    parsed = LinearAlgebraParams(**params)

    try:
        op = parsed.operation

        if op == "add":
            result = _to_matrix(parsed.matrix_a) + _to_matrix(parsed.matrix_b)
        elif op == "subtract":
            result = _to_matrix(parsed.matrix_a) - _to_matrix(parsed.matrix_b)
        elif op == "multiply":
            a = _to_matrix(parsed.matrix_a)
            if parsed.scalar is not None:
                result = safe_parse(parsed.scalar) * a
            else:
                result = a * _to_matrix(parsed.matrix_b)
        elif op == "transpose":
            result = _to_matrix(parsed.matrix_a).T
        elif op == "determinant":
            result = _to_matrix(parsed.matrix_a).det()
        elif op == "inverse":
            result = _to_matrix(parsed.matrix_a).inv()
        elif op == "eigenvalues":
            result = _to_matrix(parsed.matrix_a).eigenvals()
        elif op == "eigenvectors":
            result = _to_matrix(parsed.matrix_a).eigenvects()
        elif op == "rank":
            result = _to_matrix(parsed.matrix_a).rank()
        elif op == "solve_linear_system":
            a = _to_matrix(parsed.matrix_a)
            b = _to_matrix(parsed.matrix_b)
            result = a.solve(b)
        elif op == "dot_product":
            result = _to_vector(parsed.vector_a).dot(_to_vector(parsed.vector_b))
        elif op == "cross_product":
            va, vb = _to_vector(parsed.vector_a), _to_vector(parsed.vector_b)
            if len(va) != 3 or len(vb) != 3:
                return {"error": "cross_product requires 3-dimensional vectors"}
            result = va.cross(vb)
        elif op == "norm":
            result = _to_vector(parsed.vector_a).norm()
        else:
            return {"error": f"Unknown operation: {op}"}

    except MathParseError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    return {"result": serialize(result), "operation": op}
