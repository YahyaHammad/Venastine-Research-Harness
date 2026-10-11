"""
tools/builtin/discrete_math.py

Number theory and combinatorics: gcd/lcm, primality, factorization,
factorial, permutations, combinations, and modular arithmetic.
"""

from __future__ import annotations

from typing import Literal, Optional

import sympy
from pydantic import BaseModel, Field

from tools.builtin._math_common import MathParseError, parse_params

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
    try:
        parsed = parse_params(DiscreteMathParams, params)
    except MathParseError as e:
        return {"error": str(e)}

    try:
        op = parsed.operation

        if op == "gcd":
            if parsed.a is None or parsed.b is None:
                return {"error": "gcd requires 'a' and 'b' (both integers)."}
            result = sympy.gcd(parsed.a, parsed.b)
        elif op == "lcm":
            if parsed.a is None or parsed.b is None:
                return {"error": "lcm requires 'a' and 'b' (both integers)."}
            result = sympy.lcm(parsed.a, parsed.b)
        elif op == "is_prime":
            if parsed.n is None:
                return {"error": "is_prime requires 'n' (integer)."}
            result = sympy.isprime(parsed.n)
        elif op == "prime_factors":
            if parsed.n is None:
                return {"error": "prime_factors requires 'n' (integer)."}
            result = sympy.factorint(parsed.n)  # {prime: exponent}
        elif op == "next_prime":
            if parsed.n is None:
                return {"error": "next_prime requires 'n' (integer)."}
            result = sympy.nextprime(parsed.n)
        elif op == "factorial":
            if parsed.n is None:
                return {"error": "factorial requires 'n' (integer)."}
            result = sympy.factorial(parsed.n)
        elif op == "permutations":
            # nPr AS THE FALLING FACTORIAL, which is O(r). It used to be
            # `factorial(n) / factorial(n - r)`, so its cost was in `n`
            # while the operation's cost is in `r` -- and `combinations`,
            # four lines below in this same chain, was already O(r) via
            # `binomial`. Measured, with `combinations` on identical
            # inputs as the control in every row:
            #
            #     permutations n=100000   r=1    0.14s
            #     permutations n=1000000  r=1   15.27s of a 20s budget
            #     permutations n=3000000  r=1   killed at the wall clock,
            #                                   48.07s uncontained
            #     combinations n=3000000  r=1    0.0002s -> 3000000
            #
            # At r=1 the two operations return the same number, and the
            # model was told "the inputs given are too large for this
            # operation" about one multiplication. `ff` agrees with the
            # factorial form everywhere both are defined, checked
            # including the edges: ff(5,7)=0 (the old form divided by
            # factorial(-2)=zoo) and ff(10,-1)=1/11.
            #
            # This is H5's trade -- paying a full budget to learn the
            # answer is the wrong answer -- and it does NOT contradict H4,
            # which refuses a per-parameter CEILING. The fix is an
            # algorithm. H4's rationale ("cost is joint, so no
            # per-parameter ceiling expresses it") was true of
            # `combinations` and false of this line as it stood; the
            # comment that stood here preferred a common sympy name to the
            # right complexity class.
            if parsed.n is None or parsed.r is None:
                return {"error": "permutations requires 'n' and 'r' (integers)."}
            result = sympy.ff(parsed.n, parsed.r)
        elif op == "combinations":
            if parsed.n is None or parsed.r is None:
                return {"error": "combinations requires 'n' and 'r' (integers)."}
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
            if parsed.base is None or parsed.exponent is None:
                return {"error": "modular_exponent requires 'base', 'exponent' and 'modulus' (all integers)."}
            if parsed.modulus is None:
                return {"error": "modular_exponent requires a modulus. Without one this is plain exponentiation, which can produce a number too large to compute or return. Supply modulus, or ask for a smaller exponent."}
            if parsed.modulus == 0:
                return {"error": "modular_exponent requires a non-zero modulus (modulus cannot be 0)."}
            result = pow(parsed.base, parsed.exponent, parsed.modulus)
        elif op == "mod_inverse":
            if parsed.base is None or parsed.modulus is None:
                return {"error": "mod_inverse requires 'base' and 'modulus' (integers)."}
            if parsed.modulus == 0:
                return {"error": "mod_inverse requires a non-zero modulus (modulus cannot be 0)."}
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
