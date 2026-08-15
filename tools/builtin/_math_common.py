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

A THIRD mechanism killed the first fix, and is why the guard looks the
way it does. That fix filtered NAME *tokens* against an allowlist. It was
bypassed within the hour:

    f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"   -> the pid

On Python 3.11 an f-string is a SINGLE `STRING` token -- PEP 701 only
split it into `FSTRING_*` tokens in 3.12 -- so a NAME-token filter is
structurally blind to everything inside one, while the code inside is
evaluated with `_SAFE_GLOBALS` in scope, `sympify` included. `str.format`
reaches dunder attributes the same way, through its own mini-language.

The lesson generalises past f-strings: **a token stream is not the
language**, and which constructs the tokenizer hides is a property of the
Python version rather than of this code.

So the guard is `_validate_ast`, an allowlist over AST NODE TYPES applied
to exactly the string that will be evaluated -- see the long comment
above it. `parse_expr` is `stringify_expr` followed by `eval_expr`, and
the check sits between them.

One thing NOT to rely on: today the 26 submodule objects in
`_SAFE_GLOBALS` (`external.gmpy.os`, `printing.gtk.subprocess`,
`parsing.sympy_parser.builtins`, ...) happen to be unreachable, because
`auto_symbol` leaves a name alone only when it is a Basic, a type, or
callable -- and a module is none of the three, so it is rewritten to
`Symbol('external')`. That is an undocumented SymPy implementation detail
protecting a real hole. Attribute access is refused outright and the name
allowlist excludes every module, so nothing depends on it.
"""

from __future__ import annotations

import ast
from typing import Any, Optional

import sympy
from sympy.parsing.sympy_parser import (
    eval_expr,
    standard_transformations,
    stringify_expr,
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
    N nsimplify
""".split())


# implicit_multiplication_application lets "2x" parse as "2*x".
# convert_equals_signs lets "x**2 - 4 = 0" parse into an Eq(...) object
# instead of raising a syntax error on the bare "=".
_TRANSFORMATIONS = standard_transformations + (
    implicit_multiplication_application,
    convert_equals_signs,
)


# ---------------------------------------------------------------------------
# ---- The guard: an AST allowlist over the code that will be EVALUATED ------
# ---------------------------------------------------------------------------
#
# WHY NOT A TOKEN FILTER. The first attempt at #52 rejected dunder and
# non-allowlisted NAME *tokens*. It was bypassed within the hour:
#
#     f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"   -> the pid
#
# On Python 3.11 an f-string is a SINGLE `STRING` token (PEP 701 only
# split it into `FSTRING_*` tokens in 3.12), so a NAME-token filter is
# structurally blind to everything inside one -- and the code inside is
# evaluated with `_SAFE_GLOBALS` in scope, `sympify` included.
# `"{0.__class__.__bases__}".format(pi)` reaches dunder attributes the
# same way, through `str.format`'s own mini-language.
#
# The lesson generalises past f-strings: a token stream is not the
# language. Any construct whose contents the tokenizer does not expose as
# tokens is invisible to a token filter, and "which constructs are those"
# is a property of the Python version, not of this code. That is not
# something a filter can be argued sound against.
#
# So the guard validates the ABSTRACT SYNTAX TREE of exactly the string
# that gets evaluated. `parse_expr` is `stringify_expr` (apply the
# transformations, emit code) followed by `eval_expr` (eval it), so
# splitting it open puts the check between those two steps -- after
# "2x" has become `Integer(2)*Symbol('x')`, and before anything runs.
#
# What that buys, and it is the whole point: the allowed set is CLOSED
# over node types. A construct nobody thought of -- an f-string, a walrus,
# a comprehension, a lambda, a generator, whatever a future Python adds --
# is refused because it is not on the list, not because someone
# remembered it.

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Call, ast.keyword,
    ast.Name, ast.Load,
    ast.Constant,
    ast.Tuple, ast.List,
    ast.Subscript, ast.Slice,
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.UAdd, ast.USub, ast.Not, ast.Invert,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)

# `ast.Attribute` is deliberately ABSENT, and it is the single most
# load-bearing omission. Attribute access IS #52's mechanism 2 --
# `().__class__.__bases__[0].__subclasses__()` needs no name lookup at
# all -- and it is also how `str.format` and every bound-method route is
# reached. No transformation emits an attribute for a legitimate
# expression, so nothing needs it: the functional forms (`diff(f, x)`
# rather than `f.diff(x)`) are what the tools take anyway.


# The only two calls `stringify_expr` gives a string argument to. Kept
# minimal on purpose: each entry is a callable that must be argued not to
# sympify what it is handed.
_STRING_ARG_CONSTRUCTORS = frozenset({"Symbol", "Float"})


def _validate_ast(code: str, local_dict: dict) -> None:
    """Refuse anything outside the closed node allowlist. Raises
    MathParseError; returns None when the expression is acceptable."""
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as e:
        raise MathParseError(f"Could not parse expression: {e}") from e

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise MathParseError(
                f"{type(node).__name__} is not allowed in a math "
                f"expression")

        # Every name is resolved against _SAFE_GLOBALS at eval time, so
        # every name must be one we chose. Unknown names never reach here
        # -- auto_symbol has already rewritten them to Symbol('name') --
        # except the ones the CALLER declared via `symbols=`, which is
        # what local_dict holds.
        if isinstance(node, ast.Name):
            if node.id not in _ALLOWED_NAMES and node.id not in local_dict:
                raise MathParseError(
                    f"'{node.id}' is not a permitted function or "
                    f"constant. Use a mathematical function, or a plain "
                    f"name for a variable.")

    # A STRING may appear only where the transformations put one.
    #
    # Seven allowlisted functions call `sympify()` on their own arguments
    # -- `factor`, `cancel`, `together`, `apart`, `roots`, `degree`,
    # `nsolve` -- which was found by auditing the allowlist's source
    # rather than by guessing. Handing any of them a string literal
    # re-enters the parser with SymPy's own default globals, real
    # builtins included, and that is #52's mechanism 1 through a side
    # door:
    #
    #     factor(" __import__(chr(111)+chr(115)).getpid() ")   -> the pid
    #
    # The leading space matters: an earlier version of this guard only
    # tripped on a string that *started or ended* with a dunder, so one
    # space defeated it. Prefix matching on a payload is the same
    # category of mistake as `chr(111)+chr(115)` defeating a search for
    # the literal "os".
    #
    # `stringify_expr` emits string arguments in exactly two places --
    # `Symbol('x')` for every free variable and `Float('1.5')` for every
    # non-integer literal -- so licensing those two and refusing every
    # other string keeps the property closed rather than filtered. This
    # also removes `"%s" % pi` and every other string-valued construct
    # without needing a rule per construct.
    licensed = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _STRING_ARG_CONSTRUCTORS):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    licensed.add(id(arg))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in licensed):
            raise MathParseError(
                "string literals are not allowed in a math expression")


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
        # parse_expr() split open, so the guard can sit between the two
        # halves -- see _validate_ast. `evaluate=False` is not used
        # anywhere here, which is the only other thing parse_expr does.
        code = stringify_expr(expr_str, local_dict, _SAFE_GLOBALS,
                              _TRANSFORMATIONS)
        _validate_ast(code, local_dict)
        return eval_expr(code, local_dict, _SAFE_GLOBALS)
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
