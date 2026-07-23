"""
tools/builtin/discrete_math.py

Number theory and combinatorics: gcd/lcm, primality, factorization,
factorial, permutations, combinations, and modular arithmetic.
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from pydantic import BaseModel, Field

_OPERATIONS = Literal[
    "gcd", "lcm", "is_prime", "prime_factors", "next_prime",
    "factorial", "permutations", "combinations",
    "modular_exponent", "mod_inverse",
]


class DiscreteMathParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    a: Optional[int] = Field(None, description="First integer, for gcd/lcm")
    b: Optional[int] = Field(None, description="Second integer, for gcd/lcm")
    n: Optional[int] = Field(None, description="n, for is_prime/prime_factors/next_prime/factorial/permutations/combinations")
    r: Optional[int] = Field(None, description="r, for permutations (nPr) / combinations (nCr)")
    base: Optional[int] = Field(None, description="base, for modular_exponent/mod_inverse")
    exponent: Optional[int] = Field(None, description="exponent, for modular_exponent")
    modulus: Optional[int] = Field(None, description="modulus, for modular_exponent/mod_inverse")


TOOL_SCHEMA = {
    "name": "discrete_math",
    "description": (
        "Number theory and combinatorics: gcd, lcm, primality testing, "
        "prime factorization, next prime, factorial, permutations (nPr), "
        "combinations (nCr), modular exponentiation, and modular inverse."
    ),
    "input_schema": DiscreteMathParams.model_json_schema(),
}


def run(params: dict) -> dict:
    parsed = DiscreteMathParams(**params)

    try:
        op = parsed.operation

        if op == "gcd":
            result = sympy.gcd(parsed.a, parsed.b)
        elif op == "lcm":
            result = sympy.lcm(parsed.a, parsed.b)
        elif op == "is_prime":
            result = sympy.isprime(parsed.n)
        elif op == "prime_factors":
            result = sympy.factorint(parsed.n)  # {prime: exponent}
        elif op == "next_prime":
            result = sympy.nextprime(parsed.n)
        elif op == "factorial":
            result = sympy.factorial(parsed.n)
        elif op == "permutations":
            # nPr = n! / (n-r)! -- computed directly rather than relying
            # on a less-common sympy function name.
            result = sympy.factorial(parsed.n) / sympy.factorial(parsed.n - parsed.r)
        elif op == "combinations":
            result = sympy.binomial(parsed.n, parsed.r)
        elif op == "modular_exponent":
            result = pow(parsed.base, parsed.exponent, parsed.modulus)
        elif op == "mod_inverse":
            result = sympy.mod_inverse(parsed.base, parsed.modulus)
        else:
            return {"error": f"Unknown operation: {op}"}

    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    if isinstance(result, dict):
        result = {str(k): v for k, v in result.items()}
        return {"result": result, "operation": op}

    return {"result": str(result), "operation": op}
