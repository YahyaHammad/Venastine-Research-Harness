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
        "linear systems (Ax=b where b is a column vector/matrix with the same "
        "number of rows as A, e.g. [[7],[5]] for a 2-row system), dot product, "
        "cross product, and norm. Entries can be exact numbers or symbolic "
        "expressions."
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
            if parsed.matrix_a is None or parsed.matrix_b is None:
                return {"error": "add requires 'matrix_a' and 'matrix_b' (each a list of rows)."}
            result = _to_matrix(parsed.matrix_a) + _to_matrix(parsed.matrix_b)
        elif op == "subtract":
            if parsed.matrix_a is None or parsed.matrix_b is None:
                return {"error": "subtract requires 'matrix_a' and 'matrix_b' (each a list of rows)."}
            result = _to_matrix(parsed.matrix_a) - _to_matrix(parsed.matrix_b)
        elif op == "multiply":
            if parsed.matrix_a is None:
                return {"error": "multiply requires 'matrix_a'."}
            a = _to_matrix(parsed.matrix_a)
            if parsed.scalar is not None:
                result = safe_parse(parsed.scalar) * a
            else:
                if parsed.matrix_b is None:
                    return {"error": "multiply requires 'matrix_b' or 'scalar'."}
                result = a * _to_matrix(parsed.matrix_b)
        elif op == "transpose":
            if parsed.matrix_a is None:
                return {"error": "transpose requires 'matrix_a'."}
            result = _to_matrix(parsed.matrix_a).T
        elif op == "determinant":
            if parsed.matrix_a is None:
                return {"error": "determinant requires 'matrix_a'."}
            m = _to_matrix(parsed.matrix_a)
            if m.rows != m.cols:
                return {"error": f"determinant requires a square matrix (got {m.rows}x{m.cols})."}
            result = m.det()
        elif op == "inverse":
            if parsed.matrix_a is None:
                return {"error": "inverse requires 'matrix_a'."}
            m = _to_matrix(parsed.matrix_a)
            if m.rows != m.cols:
                return {"error": f"inverse requires a square matrix (got {m.rows}x{m.cols})."}
            result = m.inv()
        elif op == "eigenvalues":
            if parsed.matrix_a is None:
                return {"error": "eigenvalues requires 'matrix_a'."}
            m = _to_matrix(parsed.matrix_a)
            if m.rows != m.cols:
                return {"error": f"eigenvalues requires a square matrix (got {m.rows}x{m.cols})."}
            result = m.eigenvals()
        elif op == "eigenvectors":
            if parsed.matrix_a is None:
                return {"error": "eigenvectors requires 'matrix_a'."}
            m = _to_matrix(parsed.matrix_a)
            if m.rows != m.cols:
                return {"error": f"eigenvectors requires a square matrix (got {m.rows}x{m.cols})."}
            result = m.eigenvects()
        elif op == "rank":
            if parsed.matrix_a is None:
                return {"error": "rank requires 'matrix_a'."}
            result = _to_matrix(parsed.matrix_a).rank()
        elif op == "solve_linear_system":
            if parsed.matrix_a is None or parsed.matrix_b is None:
                return {"error": "solve_linear_system requires 'matrix_a' and 'matrix_b' (b as column vector/matrix with same rows as A, e.g. [[7],[5]])."}
            a = _to_matrix(parsed.matrix_a)
            b = _to_matrix(parsed.matrix_b)
            if a.rows != b.rows:
                return {"error": f"solve_linear_system: 'matrix_b' has {b.rows} rows but 'matrix_a' has {a.rows} — b must be a column vector/matrix with the same number of rows as A (e.g. [[7],[5]] for a 2-row system)."}
            result = a.solve(b)
        elif op == "dot_product":
            if parsed.vector_a is None or parsed.vector_b is None:
                return {"error": "dot_product requires 'vector_a' and 'vector_b'."}
            result = _to_vector(parsed.vector_a).dot(_to_vector(parsed.vector_b))
        elif op == "cross_product":
            if parsed.vector_a is None or parsed.vector_b is None:
                return {"error": "cross_product requires 'vector_a' and 'vector_b'."}
            va, vb = _to_vector(parsed.vector_a), _to_vector(parsed.vector_b)
            if len(va) != 3 or len(vb) != 3:
                return {"error": "cross_product requires 3-dimensional vectors"}
            result = va.cross(vb)
        elif op == "norm":
            if parsed.vector_a is None:
                return {"error": "norm requires 'vector_a'."}
            result = _to_vector(parsed.vector_a).norm()
        else:
            return {"error": f"Unknown operation: {op}"}

    except MathParseError as e:
        return {"error": str(e)}
    except Exception as e:
        # SymPy's NonSquareMatrixError has an empty string, which previously
        # surfaced as "Computation failed: " with nothing after the colon.
        if type(e).__name__ == "NonSquareMatrixError" or "NonSquareMatrix" in type(e).__name__:
            return {"error": "Computation failed: determinant/inverse/eigen requires a square matrix — the supplied matrix is not square."}
        return {"error": f"Computation failed: {e}"}

    return {"result": serialize(result), "operation": op}
