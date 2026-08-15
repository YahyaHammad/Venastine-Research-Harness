"""
tools/builtin/_math_common.py

Shared safe-parsing foundation for every math tool. Prefixed with an
underscore since this isn't itself a registered tool -- it's imported by
the six math tools that are.

SAFETY NOTE: SymPy's parser (sympify/parse_expr) tokenizes and then calls
Python's eval() on the result, so an expression string is code. Every one
of them arrives from the model, which during the grounding passes has been
reading pages it did not author, and all six math tools are
`permission=True, approval=False` -- advertised and callable with no
prompt in plain chat AND in all ten research passes.

BLANKING `__builtins__` IS NOT SUFFICIENT, AND CANNOT BE (#52). That was
this module's original premise and it was wrong in two independent ways:

  1. `_SAFE_GLOBALS` is built from `dir(sympy)`, so SymPy's OWN parser
     entry points are in the namespace by construction -- `sympify`,
     `parse_expr`, `var`, `init_printing`, `S`, `lambdify`. `sympify`'s
     inner `parse_expr` builds its own globals via
     `exec('from sympy import *', global_dict)`, and Python inserts REAL
     `__builtins__` into any globals dict handed to exec/eval that lacks
     it. The outer blanking never applies to the inner parse.
  2. `().__class__.__bases__[0].__subclasses__()` needs no builtins at
     all -- only attribute access on a literal -- and yields hundreds of
     live classes. No namespace pruning can stop that.

So the guard is an ALLOWLIST at the token boundary (`_reject_unsafe_names`
below) rather than a denylist in the namespace. It runs as the FIRST
transformation, before `auto_symbol` rewrites anything, so it sees the
names the caller actually wrote.

One thing NOT to rely on: today the 26 submodule objects in
`_SAFE_GLOBALS` (`external.gmpy.os`, `printing.gtk.subprocess`,
`parsing.sympy_parser.builtins`, ...) happen to be unreachable, because
`auto_symbol` leaves a name alone only when it is a Basic, a type, or
callable -- and a module is none of the three, so it is rewritten to
`Symbol('external')`. That is an undocumented SymPy implementation detail
protecting a real hole. The allowlist covers those names directly, so the
protection does not depend on it.
"""

from __future__ import annotations

from tokenize import NAME
from typing import Any, Optional

import sympy
from sympy.parsing.sympy_parser import (
    parse_expr,
    standard_transformations,
    implicit_multiplication_application,
    convert_equals_signs,
)

# SymPy's own public namespace (sin, cos, sqrt, pi, Symbol, Matrix, oo, ...)
# with __builtins__ explicitly neutered. Built once at import time.
#
# The blanking is kept -- it is not what makes this safe (see the module
# docstring), but removing it would re-open the one payload shape the
# original test covered, for no gain.
_SAFE_GLOBALS: dict[str, Any] = {
    name: getattr(sympy, name) for name in dir(sympy) if not name.startswith("_")
}
_SAFE_GLOBALS["__builtins__"] = {}


class MathParseError(ValueError):
    """Raised when an expression string can't be safely parsed."""


# What a math EXPRESSION may name. Grouped by category rather than listed
# flat, so that widening it is a visible decision about a KIND of thing
# rather than a name appended to a pile.
#
# The rule for admitting a name: it computes over SymPy objects. Anything
# that parses, evaluates, compiles, renders or executes stays out --
# `sympify`, `parse_expr`, `var`, `S`, `lambdify`, `init_printing`,
# `preview`, and the 26 submodule objects, all of which are reachable
# names in `_SAFE_GLOBALS` and none of which a math expression needs.
#
# Being CLOSED is the property that matters. A future SymPy that adds a
# new evaluating callable, or that stops rewriting bare module names into
# Symbols, is refused by default rather than newly exposed. That is the
# whole reason this is an allowlist and not a denylist of known-bad names.
_ALLOWED_NAMES = frozenset("""
    sin cos tan cot sec csc sinc asin acos atan atan2 acot asec acsc
    sinh cosh tanh coth sech csch asinh acosh atanh acoth asech acsch
    exp log ln sqrt cbrt root Abs sign floor ceiling frac Mod
    factorial factorial2 subfactorial binomial gamma lowergamma uppergamma
    loggamma digamma trigamma polygamma beta
    erf erfc erfi erfinv erf2 zeta dirichlet_eta lerchphi polylog
    LambertW Ei li Li Si Ci Shi Chi expint
    besselj bessely besseli besselk hankel1 hankel2 airyai airybi
    jacobi gegenbauer chebyshevt chebyshevu legendre assoc_legendre
    hermite laguerre assoc_laguerre hyper meijerg elliptic_k elliptic_e
    conjugate re im arg Add Mul Pow
    fibonacci lucas tribonacci catalan harmonic bernoulli euler
    primepi prime isprime nextprime prevprime factorint divisors totient
    gcd lcm igcd ilcm mod_inverse
    pi E I oo zoo nan GoldenRatio EulerGamma Catalan TribonacciConstant
    true false Integer Float Rational Symbol Tuple Dict
    Matrix ImmutableMatrix eye zeros ones diag
    Interval FiniteSet Union Intersection Complement EmptySet
    Range ImageSet ConditionSet ProductSet Naturals Integers Reals
    Eq Ne Lt Le Gt Ge StrictLessThan StrictGreaterThan
    And Or Not Xor Nand Nor Implies Equivalent ITE
    diff integrate limit Sum Product Derivative Integral Limit Order
    simplify expand factor together apart cancel collect
    trigsimp radsimp powsimp logcombine expand_trig expand_log
    solve solveset roots nsolve dsolve linsolve nonlinsolve
    Poly degree LC LT rem quo div
    transpose det trace adjugate
    Function Lambda Piecewise Max Min Heaviside DiracDelta KroneckerDelta
    Point Point2D Point3D Line Segment Ray Circle Ellipse Polygon Triangle
    N evalf nsimplify
""".split())


def _reject_unsafe_names(tokens, local_dict, global_dict):
    """First transformation in the pipeline: refuse the payload class.

    Runs BEFORE `standard_transformations`, so it sees the names the
    caller wrote rather than what `auto_symbol` rewrote them into. A name
    that is NOT in `_SAFE_GLOBALS` is left alone deliberately -- becoming
    a Symbol is exactly what an unknown name in a math expression should
    do, and it is the parser's whole purpose.

    Two rules, one per mechanism in #52:

      1. No dunder, anywhere. `().__class__.__bases__[0].__subclasses__()`
         is attribute access on a literal, so it needs no name lookup at
         all and no namespace change can reach it.
      2. No global name outside `_ALLOWED_NAMES`. This is what closes
         `sympify` re-entry, and it closes `lambdify`, `S`, `var`,
         `init_printing` and the submodule routes at the same time,
         because it never enumerated them.
    """
    for toknum, tokval in tokens:
        if toknum != NAME:
            continue
        if tokval.startswith("__") or tokval.endswith("__"):
            raise MathParseError(
                f"'{tokval}' is not allowed in a math expression")
        if tokval in _SAFE_GLOBALS and tokval not in _ALLOWED_NAMES:
            raise MathParseError(
                f"'{tokval}' is not a permitted function or constant. "
                f"Use a mathematical function, or a plain name for a "
                f"variable.")
    return tokens


# _reject_unsafe_names FIRST -- see its docstring.
# implicit_multiplication_application lets "2x" parse as "2*x".
# convert_equals_signs lets "x**2 - 4 = 0" parse into an Eq(...) object
# instead of raising a syntax error on the bare "=".
_TRANSFORMATIONS = (_reject_unsafe_names,) + standard_transformations + (
    implicit_multiplication_application,
    convert_equals_signs,
)


def safe_parse(expr_str: str, symbols: Optional[list[str]] = None):
    """
    Parses a math expression string into a SymPy object without exposing
    Python builtins. `symbols` is optional -- SymPy's standard
    transformations already auto-convert undefined names into Symbols,
    but passing them explicitly keeps variable identity consistent across
    multiple calls (e.g. the same 'x' in an expression and a separate
    bound for a definite integral).
    """
    local_dict = {name: sympy.Symbol(name) for name in (symbols or [])}
    try:
        return parse_expr(
            expr_str,
            local_dict=local_dict,
            global_dict=_SAFE_GLOBALS,
            transformations=_TRANSFORMATIONS,
        )
    except MathParseError:
        # A refusal from _reject_unsafe_names already says which name was
        # refused and what to do instead. Re-wrapping it in "Could not
        # parse expression '<the whole payload>'" buries that under the
        # string the model just sent, and echoes the payload back into
        # the transcript for no reason.
        raise
    except Exception as e:
        raise MathParseError(f"Could not parse expression '{expr_str}': {e}") from e


def serialize(value: Any) -> Any:
    """
    Generic SymPy-object -> JSON-safe converter, used across all six math
    tools. Matrices become nested lists of strings, dicts/lists recurse,
    everything else falls back to str().
    """
    if isinstance(value, sympy.Matrix):
        return [[str(cell) for cell in row] for row in value.tolist()]
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    if isinstance(value, bool):
        return value
    return str(value)
