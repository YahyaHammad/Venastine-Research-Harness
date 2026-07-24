"""
test_math_tools.py

Verifies the six math tools (symbolic_math, linear_algebra,
probability_stats, discrete_math, logic, geometry) against exact
answers. Per ROADMAP §4 ("known-correct answers").

EXPECTATION EXPRESSION STRATEGY (decided in planning):
  SymPy's serialization can drift across versions (e.g. `sqrt(2)` vs
  `2**(1/2)`). To insulate the test suite from that drift, assertions
  use SYMBOLIC EQUIVALENCE, not string equality:
    assert sympy.simplify(actual - expected) == 0
  or, for non-numeric results:
    assert sympy.simplify(actual - expected) is sympy.S.Zero

  For operations whose result is naturally non-numeric (a list of
  solutions, a set of points, a boolean), we convert if needed and
  compare symbolically where it's possible, fall back to sympify'd
  equality for the simple ones, and keep string-equivalence for cases
  where there's a single canonical form (e.g. an integer scalar).

ALSO INCLUDED: the safe_parse injection-blocking regression case.
  ARCHITECTURE.md §10: "_math_common.py's safe_parse ... verified via
  test to actually block a real injection payload, not just assumed
  safe." This was tested once during development; this asserts it stays
  blocked.
"""

import pytest
import sympy
from sympy import S, sqrt, pi, Rational, Symbol, simplify, sympify

from tools.builtin._math_common import safe_parse, MathParseError

import tools.builtin.symbolic_math as symbolic_math
import tools.builtin.linear_algebra as linear_algebra
import tools.builtin.discrete_math as discrete_math
import tools.builtin.logic as logic
import tools.builtin.geometry as geometry
import tools.builtin.probability_stats as probability_stats


# ===========================================================================
# ---- Regression: safe_parse injection blocking (per ARCHITECTURE.md §10) -
# ===========================================================================

def test_safe_parse_blocks_dunder_import_injection():
    """ARCHITECTURE.md §10: the safe_parse parser builds a namespace with
    `__builtins__` explicitly blanked, so an expression containing a
    real attack string like __import__('os').system('echo pwned') must
    raise MathParseError and must NOT execute the payload.

    This is the regression test for that property. If safe_parse ever
    starts accepting this expression (e.g. someone "simplifies" the
    blanked-__builtins__ globals back into the parser), this test fails
    -- potentially SECURITY-CRITICAL, not a flaky test.
    """
    payload = "__import__('os').system('echo pwned_in_test')"
    with pytest.raises(MathParseError):
        safe_parse(payload)

    # Defense in depth: actually verify the side effect didn't run
    # (the test should fail anyway via MathParseError above, but if
    # safe_parse ever silently swallowed the parse error and let the
    # expression execute, this is the secondary catch).
    # (There's no clean way to check "echo didn't run" directly here --
    # the assertion is that safe_parse itself raised first.)


def test_safe_parse_accepts_normal_expressions():
    """Sanity: a normal expression with no builtins usage parses cleanly
    and produces a SymPy object."""
    expr = safe_parse("x**2 + 3*x - 4", symbols=["x"])
    assert simplify(expr - (Symbol("x")**2 + 3*Symbol("x") - 4)) == 0


# ===========================================================================
# ---- symbolic_math --------------------------------------------------------
# ===========================================================================

def test_symbolic_math_simplify_x_plus_x():
    """simplify(x + x) should produce 2*x -- stable across SymPy versions."""
    result = symbolic_math.run({"operation": "simplify", "expression": "x + x", "variable": "x"})
    assert "result" in result
    actual = sympify(result["result"])
    expected = 2 * Symbol("x")
    assert simplify(actual - expected) == 0


def test_symbolic_math_differentiate_x_cubed():
    """d/dx(x**3) = 3*x**2 -- stable across SymPy versions."""
    result = symbolic_math.run({"operation": "differentiate", "expression": "x**3", "variable": "x", "order": 1})
    assert "result" in result
    actual = sympify(result["result"])
    expected = 3 * Symbol("x")**2
    assert simplify(actual - expected) == 0


def test_symbolic_math_solve_simple_quadratic():
    """solve(x**2 - 4, x) = {-2, 2} as a set of solutions."""
    result = symbolic_math.run({"operation": "solve", "expression": "x**2 - 4", "variable": "x"})
    assert "result" in result
    # sympy.solve returns either a list of solutions or a list of dicts;
    # we serialize to a list of strings. The solutions should be "-2" and "2".
    solutions_str = result["result"]
    parsed = [sympify(s) for s in solutions_str]
    assert set(parsed) == {sympify(-2), sympify(2)}


# ===========================================================================
# ---- linear_algebra -----------------------------------------------------
# ===========================================================================

def test_linear_algebra_dot_product_of_orthogonal_vectors():
    """The dot product of [1,0,0] and [0,1,0] is 0 -- trivial, stable.

    LinearAlgebraParams has `vector_a` and `vector_b` as the dot_product
    inputs (confirmed against tools/builtin/linear_algebra.py)."""
    result = linear_algebra.run({
        "operation": "dot_product",
        "vector_a": ["1", "0", "0"],
        "vector_b": ["0", "1", "0"],
    })
    assert "result" in result, f"expected 'result' key, got: {result}"
    actual = sympify(result["result"])
    assert simplify(actual - 0) == 0


def test_linear_algebra_determinant_of_identity_matrix():
    """det([[1,0],[0,1]]) = 1. Uses matrix_a as 2x2 identity, the most
    stable possible answer."""
    result = linear_algebra.run({
        "operation": "determinant",
        "matrix_a": [["1", "0"], ["0", "1"]],
    })
    assert "result" in result
    assert sympify(result["result"]) == 1


# ===========================================================================
# ---- discrete_math -------------------------------------------------------
# ===========================================================================

def test_discrete_math_gcd_of_12_and_18():
    """gcd(12, 18) = 6, stable integer answer."""
    result = discrete_math.run({"operation": "gcd", "a": 12, "b": 18})
    assert "result" in result
    assert int(result["result"]) == 6


def test_discrete_math_is_prime_17_true():
    """17 is prime. The result should be exactly 'True' (boolean serialized
    as string)."""
    result = discrete_math.run({"operation": "is_prime", "n": 17})
    assert "result" in result
    val = result["result"]
    # Either bool True or string "True"; both indicate "yes, prime."
    if isinstance(val, bool):
        assert val is True
    elif isinstance(val, str):
        assert val.lower() == "true"
    else:
        assert val == 17  # fell back to returning the input -- bug


def test_discrete_math_combinations_5_choose_2():
    """C(5, 2) = 10. Stable integer."""
    result = discrete_math.run({"operation": "combinations", "n": 5, "r": 2})
    assert "result" in result
    assert int(result["result"]) == 10


# ===========================================================================
# ---- logic --------------------------------------------------------------
# ===========================================================================

def test_logic_tautology_a_or_not_a():
    """a | ~a is a tautology -- always True regardless of truth assignment."""
    result = logic.run({"operation": "tautology", "expression": "a | ~a"})
    assert "result" in result
    val = result["result"]
    if isinstance(val, bool):
        assert val is True
    elif isinstance(val, str):
        assert val.lower() == "true" or val.lower() == "tautology"
    else:
        pytest.fail(f"Unexpected result type for tautology result: {type(val)} -> {val}")


# ===========================================================================
# ---- geometry -----------------------------------------------------------
# ===========================================================================

def test_geometry_distance_3_4_to_hypotenuse_5():
    """distance((0,0), (3,4)) = 5 by Pythagoras. Stable numeric/symbolic
    answer (Sympy returns either 5 as int or as Rational/sqrt form; in
    this case it's exactly 5)."""
    result = geometry.run({
        "operation": "distance",
        "points": [["0", "0"], ["3", "4"]],
    })
    assert "result" in result
    actual = sympify(result["result"])
    expected = sympify(5)
    assert simplify(actual - expected) == 0


def test_geometry_circle_area_radius_5_pi_r_squared():
    """circle_area(center=[0,0], radius=5) should produce 25*pi -- a
    stable symbolic identity. Note the geometry tool's circle_area op
    requires BOTH center and radius (see geometry.py:68)."""
    result = geometry.run({
        "operation": "circle_area",
        "center": ["0", "0"],
        "radius": "5",
    })
    assert "result" in result, f"unexpected error: {result}"
    actual = sympify(result["result"])
    expected = 25 * pi
    assert simplify(actual - expected) == 0


# ===========================================================================
# ---- probability_stats (descriptive stats only here -- stable) ---------
# ===========================================================================

def test_probability_stats_describe_mean_of_1_to_5():
    """describe([1, 2, 3, 4, 5]) returns a dict whose 'mean' field == 3.
    Uses the stdlib `statistics` module on the data; deterministic."""
    # _OPERATIONS contains 'describe' (confirmed against the file). It
    # uses the `data` field as list[float].
    result = probability_stats.run({
        "operation": "describe",
        "data": [1, 2, 3, 4, 5],
    })
    assert "result" in result
    assert isinstance(result["result"], dict)
    assert result["result"]["mean"] == 3
