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
            # H5. `pow(b, e, None)` is PLAIN exponentiation, and modulus
            # is Optional -- so omitting one field silently turned this
            # into a different operation with a different complexity
            # class. Measured: exponent=10**9 with a modulus is 0.000s;
            # without one it is 3.3s and a 125 MB integer, and the model
            # chooses the arguments.
            #
            # An ERROR naming the field, not a silent plain power and not
            # a separate `power` operation -- the second would advertise
            # the unbounded path rather than close it. The tool's name is
            # its contract, and H2's clock is a backstop for the runaway
            # rather than a licence to keep offering it.
            if parsed.modulus is None:
                return {"error": "modular_exponent requires a modulus. Without one this is plain exponentiation, which can produce a number too large to compute or return. Supply modulus, or ask for a smaller exponent."}
            result = pow(parsed.base, parsed.exponent, parsed.modulus)
        elif op == "mod_inverse":
            result = sympy.mod_inverse(parsed.base, parsed.modulus)
        else:
            return {"error": f"Unknown operation: {op}"}

    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    # H6. The str() below is the SECOND place this function can fail, and
    # it used to sit outside the try above -- so a correct answer that
    # was merely too large to print left the handler as a bare
    # ValueError, reached dispatch's containment, and was logged with a
    # full traceback as though it were a bug in the harness. CPython caps
    # int->str at 4300 digits, so `factorial n=100000` reaches this on
    # ordinary input.
    #
    # Contained here rather than left to dispatch because dispatch's
    # backstop exists for real bugs, and filling it with routine outcomes
    # is how a real one stops being noticed. The message also has to tell
    # the model something it can act on; the generic wrapper does not.
    try:
        if isinstance(result, dict):
            return {"result": {str(k): v for k, v in result.items()},
                    "operation": op}
        return {"result": str(result), "operation": op}
    except ValueError as e:
        return {"error": f"The {op} result was computed but is too large to return ({e}). Ask for the same operation with smaller inputs."}
