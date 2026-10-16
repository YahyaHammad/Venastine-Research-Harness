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
import json
import re
import types
from typing import Any, Optional

import sympy
from pydantic import ValidationError
from sympy.parsing.sympy_parser import (
    convert_equals_signs,
    eval_expr,
    implicit_multiplication_application,
    standard_transformations,
    stringify_expr,
)

import config

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
    """Raised when an expression string can't be safely parsed.

    ALSO THE BASE OF THE TWO BELOW, neither of which is a parse failure.
    They are in this family for one mechanical reason: every tool already
    has exactly one `except MathParseError as e: return {"error": str(e)}`
    arm, so a subclass is caught by a handler that is already there and
    already tested. Two new `except` arms across six tools would be twelve
    places to add and twelve chances to miss one, which is the shape #96
    and EP6 are both about (batch 143).
    """


class MathResultTooLarge(MathParseError):
    """A result that was computed CORRECTLY and cannot be returned.

    Two causes, one message family, because the model's remedy is the same
    for both: CPython caps `int -> str` at 4,300 digits, and a math result
    is bounded at `config.MAX_READ_CHARS`. See `serialize`.
    """


class MathParamsError(MathParseError):
    """The tool's own params did not validate. See `parse_params`."""


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

        # `**` UNPACKING IS THE ONE CONSTRUCT A LISTED NODE SPELLS
        # (TECHNICAL_DEBT 55, batch 160). `f(*args)` is `ast.Starred`,
        # which is unlisted. `f(**mapping)` is `ast.keyword(arg=None)`, and
        # `keyword` is listed for ordinary keyword arguments, so the second
        # form walked past the node check and was refused only later, by
        # SymPy ("sin takes exactly 1 argument"). It was inert, because no
        # str-keyed mapping is reachable here, but inert because of what is
        # unreachable is not what this allowlist claims. No legitimate
        # expression needs it: measured, even ordinary keyword arguments do
        # not survive `auto_symbol` (`prec=20` is refused as a name).
        if isinstance(node, ast.keyword) and node.arg is None:
            raise MathParseError(
                "** unpacking is not allowed in a math expression")

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
    #
    # A `bytes` LITERAL IS REFUSED IN THE SAME BREATH, and that is the one
    # non-`str` type it has to be: `eval()` and `compile()` both accept
    # bytes where they reject every other non-string object, so a future
    # SymPy callable that sympifies what it is handed would land there.
    # The rule here used to name `str` alone, and `factor(b"1+1")`,
    # `b"abc"` and `roots(b"x**2-1")` were all accepted -- measured, with
    # the `str` spelling of each refused as the control. No expression
    # needs one: `stringify_expr` never emits a bytes argument, so the
    # licensing pass below deliberately licenses `str` only, which refuses
    # even `Symbol(b"x")` (batch 143).
    #
    # Naming one of the two string types is the same category of mistake
    # as the dunder tripwire above: a near-miss on a type check rather
    # than on a payload.
    licensed = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _STRING_ARG_CONSTRUCTORS):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    licensed.add(id(arg))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, (str, bytes))
                and id(node) not in licensed):
            raise MathParseError(
                "string literals are not allowed in a math expression")


def _reject_escaped_value(value: Any, _depth: int = 0) -> None:
    """Backstop: refuse a RESULT that is not a mathematical value.

    Every guard above this one is an allowlist over the *input*. This is
    the one check on the *output*, and it is deliberately a DENY of the
    escape signature rather than an allowlist: its job is to catch an
    escape nobody anticipated, at the moment it succeeds, and an
    allowlist of return types can only catch escapes whose shape was
    already imagined.

    The signature is simple. A math expression evaluates to a VALUE -- a
    number, an expression tree, a matrix, a list of solutions. It never
    evaluates to a module, to a plain function, or to a class from
    outside SymPy. Those are what every escape in #52 produced or reached
    for: `<module 'os'>`, `__subclasses__`, a bound method's `__globals__`.

    SymPy's own classes ARE allowed: `sin` is a `FunctionClass`, and
    `Function('f')` legitimately evaluates to a class. Both are subclasses
    of `Basic`, which is what separates them from `type` or `os`.
    """
    if _depth > 6:
        # FAILS CLOSED. This used to `return`, which gave the one check
        # written for an escape nobody anticipated an anticipated DEPTH:
        # `[[[os]]]` was refused and `[[[[[[[[os]]]]]]]]` was accepted
        # uninspected -- measured, with the depth-3 case as the control
        # proving the function was live. The comment that stood here
        # ("containers are shallow here") was an assertion about today's
        # return shapes inside the one function whose stated job is the
        # opposite of relying on them.
        #
        # The cap's job is to bound the WALK, which it still does. The
        # deepest legitimate return shape in the battery is `eigenvects()`
        # at 4, and `test_the_backstop_allows_every_legitimate_return_shape`
        # is what goes red if that stops being true (batch 143).
        raise MathParseError(
            "expression evaluated to a value nested more than 6 deep, "
            "which is not a mathematical value")

    if isinstance(value, types.ModuleType):
        raise MathParseError(
            "expression evaluated to a module, which is never a "
            "mathematical value")

    if isinstance(value, type):
        if not issubclass(value, (sympy.Basic, sympy.matrices.MatrixBase)):
            raise MathParseError(
                f"expression evaluated to the class {value.__name__!r}, "
                f"which is not a SymPy type")
        return

    # No types.ModuleType here: the branch above already raised for one,
    # so a second listing is unreachable and reads as if this arm were the
    # module guard.
    if isinstance(value, (types.FunctionType, types.BuiltinFunctionType,
                          types.MethodType, types.BuiltinMethodType,
                          types.MethodWrapperType)):
        raise MathParseError(
            "expression evaluated to a function or method, which is "
            "never a mathematical value")

    # Raw bytes are not a mathematical value either, and they are listed
    # here for the same reason the AST rule names them: `b'1+1'`,
    # `[b'x']` and `{"k": b'x'}` all walked past this function, because
    # bytes is not a module, a type, a function, or one of the four
    # containers below (batch 143).
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise MathParseError(
            "expression evaluated to raw bytes, which is never a "
            "mathematical value")

    if isinstance(value, dict):
        for k, v in value.items():
            _reject_escaped_value(k, _depth + 1)
            _reject_escaped_value(v, _depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_escaped_value(item, _depth + 1)


# A name token, ignoring one preceded by a word character or a dot, so the
# `e5` inside `1e5` is not read as a name the caller wrote.
_NAME_TOKEN = re.compile(r"(?<![\w.])[A-Za-z_][A-Za-z0-9_]*")


def _rewritten_names(expr_str: str, result: Any, local_dict: dict) -> set:
    """The multi-character names the caller wrote that did NOT survive the
    parse as themselves.

    `_TRANSFORMATIONS` carries `split_symbols`, so a multi-character name
    that is not in `local_dict` is rewritten into a PRODUCT of single-letter
    Symbols before anything is computed: `mass` becomes `a*m*s**2`, and
    `determinant([[mass,0],[0,mass]])` then answers `a**2*m**2*s**4` as a
    successful result naming a quantity nobody asked about (batch 131).

    Decided by comparing what was written against what came back rather than
    by reimplementing SymPy's `_token_splittable`: whatever that predicate
    does with a given spelling, a name is reported here exactly when it is
    not among the result's free symbols. Three exclusions, each for its own
    reason:

      * a ONE-character name cannot be split, so it is never interesting;
      * a name in `_ALLOWED_NAMES` is a SymPy binding the caller may well
        have meant -- `E` is Euler's number and `sin` is a function, and
        deciding that those were mistakes is not this function's business
        (the `symbols=` route is how a caller overrides one);
      * a DECLARED name is in `local_dict` and therefore already protected,
        which is the whole point of declaring it.
    """
    try:
        survived = {str(s) for s in result.free_symbols}
    except AttributeError:
        # A plain bool/int/str result has no free symbols; nothing survived,
        # which is the right answer for a name that should have.
        survived = set()
    found = set()
    for token in _NAME_TOKEN.findall(expr_str):
        if len(token) < 2 or token in _ALLOWED_NAMES or token in local_dict:
            continue
        if token not in survived:
            found.add(token)
    return found


def _split_warnings(rewritten: set, field: str = "symbols") -> list[str]:
    """One sentence per split name, for a tool's `warnings` list.

    ONE copy, shared by every tool that reports this: four lists once
    answered "what counts as a project file" and no two agreed (#96), and
    four spellings of this sentence would be the same defect. The text names
    the remedy because the model is the only caller there is, and nothing in
    any schema told it the rule until batch 131.

    `field` EXISTS BECAUSE THE REMEDY IS NOT THE SAME WORD EVERYWHERE
    (batch 143). Three tools call the declaring parameter `symbols`;
    `logic` calls it `variables`. One sentence with the field named by its
    caller is one copy; a sentence that hardcodes `symbols=` is one copy
    that is wrong for a quarter of its callers, which is worse than four.
    """
    return [
        "'%s' was read as a product of single letters, not as one symbol. "
        "Pass %s=['%s'] to keep it whole." % (name, field, name)
        for name in sorted(rewritten)
    ]


def parse_params(model, params: dict):
    """Validate one tool's params, or refuse in a sentence the model can act on.

    EVERY math tool built `Model(**params)` on the line ABOVE its `try`, so
    a field constraint that fired left `run()` as a bare
    `pydantic.ValidationError`. Measured through the real transport, with
    `operation="nope"` -- the most ordinary mistake a caller can make:

        {"error": "symbolic_math did not return a usable result (exit 1).
         ... line 263, in __init__ ... pydantic_core._pydantic_core.
         ValidationError: 1 validation error for SymbolicMathParams ..."}

    plus the same traceback at WARNING, which the TUI's
    `TranscriptLogHandler` draws on screen. That is the branch
    `test_a_crashed_child_is_NOT_reported_as_a_limit` reserves for a
    harness BUG, and it is H6's defect one line higher up the same
    function: a routine, input-shaped outcome taking the path that means
    "this tool is broken". Three of the three constraints that already
    existed -- `precision` (le=50), `order` (ge=1) and the `operation`
    `Literal` itself -- reached it.

    It is also why each tool's `else: return {"error": "Unknown
    operation"}` arm has never run: the `Literal` refuses first, so the
    readable message that arm was written to give was unreachable. This is
    where it gets given.

    Not a pydantic `ValidationError` passed through verbatim: that names
    a file, a line and a pydantic.dev URL, none of which a model can act
    on (batch 143).
    """
    try:
        return model(**params)
    except ValidationError as exc:
        raise MathParamsError(_params_message(exc)) from None


def _params_message(exc: ValidationError) -> str:
    """One sentence per rejected field, in the model's own vocabulary.

    `err["msg"]` is pydantic's own wording, which for a `Literal` lists the
    operations verbatim -- exactly what the caller needs and the one thing
    the generic transport error cannot say.
    """
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", ())) or "parameters"
        parts.append("'%s' %s" % (field, err.get("msg", "is not valid")))
    return ("The parameters were not accepted: %s. Correct the parameter "
            "and call again." % "; ".join(parts))


def safe_parse(expr_str: str, symbols: Optional[list[str]] = None,
               rewritten: Optional[set] = None):
    """
    Parses a math expression string into a SymPy object without exposing
    Python builtins.

    `symbols` IS THE ONLY WAY TO KEEP A MULTI-CHARACTER NAME WHOLE, and
    that is the load-bearing half of this parameter rather than the
    convenience it used to be documented as. `_TRANSFORMATIONS` carries
    `split_symbols`, so a name absent from `local_dict` is rewritten into a
    product of single letters before anything is computed -- `mass` parses
    as `a*m*s**2`. Declaring it binds the name to a Symbol first, so it
    survives. (It also keeps variable identity consistent across calls, e.g.
    the same 'x' in an expression and a separate bound for a definite
    integral, which is what this docstring said when `symbols=` was
    reachable from only two of the four parsing tools.)

    `rewritten`, when a set is passed, collects the names that were split
    anyway -- a caller that cannot know in advance what the model will send
    reports those rather than returning a confident answer about a
    different expression. See `_rewritten_names`. The tools put them in a
    `warnings` list, which is `probability_stats`'s existing convention for
    "keep forgiving but explain".
    """
    local_dict = {name: sympy.Symbol(name) for name in (symbols or [])}
    try:
        # parse_expr() split open, so the guard can sit between the two
        # halves -- see _validate_ast. `evaluate=False` is not used
        # anywhere here, which is the only other thing parse_expr does.
        code = stringify_expr(expr_str, local_dict, _SAFE_GLOBALS,
                              _TRANSFORMATIONS)
        _validate_ast(code, local_dict)
        result = eval_expr(code, local_dict, _SAFE_GLOBALS)
        _reject_escaped_value(result)
        if rewritten is not None:
            rewritten.update(_rewritten_names(expr_str, result, local_dict))
        return result
    except MathParseError:
        # A refusal from _validate_ast already says which name or node was
        # refused and what to do instead. Re-wrapping it in "Could not
        # parse expression '<the whole payload>'" buries that under the
        # string the model just sent, and echoes the payload back into
        # the transcript for no reason.
        raise
    except Exception as e:
        raise MathParseError(f"Could not parse expression '{expr_str}': {e}") from e


def serialize(value: Any, what: str = "") -> Any:
    """
    Generic SymPy-object -> JSON-safe converter, used across all six math
    tools -- AND THE ONE PLACE A MATH RESULT IS BOUNDED (batch 143).

    Two raises, both `MathResultTooLarge`, both caught by the one
    `except MathParseError` arm every caller already has.

    H6, AT THE PRODUCER (batch 143). `str()` of an Integer above CPython's 4,300-digit cap
    raises `ValueError`, and this function was called in the `return`
    statement -- OUTSIDE every `try` -- in `symbolic_math`,
    `linear_algebra` and `geometry`. So `series order=4000` and
    `determinant([["factorial(3000)","0"],["0","1"]])` left `run()` as a
    bare `ValueError`, were reported as `exit 1` ("did not return a usable
    result"), and handed the model 400 characters of traceback naming
    `C:\\Users\\<username>\\AppData\\Roaming\\...`. `discrete_math`
    caught it and the other three did not; the guard belongs here because
    this is what the three of them share. ROADMAP_v2 H6: *"A result the
    tool cannot serialise is the tool's own error."*

    THE CEILING (batch 143). No math tool bounded its own output. Measured,
    the only ceiling was `tools/isolation.MAX_CHILD_OUTPUT_BYTES` at 4 MB
    -- a TRANSPORT limit, 62x the premise the compaction arithmetic is
    sized from (`config.yaml`: *"max_read_chars (50,000 chars = 12,500
    proxy tokens, the largest a single bounded read can return)"*):

        truth_table, 14 free symbols, 27 chars in   786,441 proxy tokens
        series order=1000 -- the documented worst case  296,272
        TECHNICAL_DEBT 41, the arxiv result already fixed  31,733

    and `core/compaction.py` says what the headroom is for: nothing in
    this harness recovers from a prompt that overflows the window. A
    result 49x `compaction_min_headroom_tokens` cannot be contained by it.

    `config.MAX_READ_CHARS` rather than a new constant, because that is
    the number the arithmetic already names, and it is read at CALL time
    so a config change needs no reimport. Measured blast radius at 50,000:
    nothing in the legitimacy battery is refused -- `series order=200` is
    35,088 characters, `expand((x+y+z)**40)` is 27,340, every matrix,
    geometry and factorial case is under 3,000 -- and the two things that
    are refused are `series order=1000` and a truth table of 9+ symbols.
    `logic` carries its own bound because it does not come through here
    and because 2**n rows are predictable from the input, which is the H5
    move; see `_MAX_TRUTH_TABLE_SYMBOLS` there.

    REFUSE rather than truncate, per the owner's ruling: half a truth
    table is a wrong answer presented as an answer, and `discrete_math`'s
    H6 message and `isolation._child_main`'s own refusal are the wording
    family this joins rather than a second convention.
    """
    label = ("The %s result" % what) if what else "The result"
    try:
        rendered = _rendered(value)
    except ValueError as e:
        # The ONLY ValueError `_rendered` can raise: it does isinstance
        # checks, container recursion and `str()`, and `str()` of a large
        # Integer is the documented cap.
        raise MathResultTooLarge(
            "%s was computed but is too large to return (%s). Ask for the "
            "same operation with smaller inputs." % (label, e)) from None

    # The JSON length rather than the character count of the pieces: this
    # is what the child actually writes, escaping included, so the number
    # in the message is the number that would have been sent.
    size = len(json.dumps(rendered))
    ceiling = int(config.MAX_READ_CHARS)
    if size > ceiling:
        raise MathResultTooLarge(
            "%s was computed but is too large to return (%d characters, "
            "against a ceiling of %d). Ask for the same operation with "
            "smaller inputs -- fewer series terms, a lower order, a "
            "smaller matrix." % (label, size, ceiling))
    return rendered


def _rendered(value: Any) -> Any:
    """`serialize` without the bounds. Separate so the size is measured
    ONCE, on the finished structure, rather than on each recursion -- a
    cap applied per level would pass a 4 MB matrix of one-character
    cells."""
    if isinstance(value, sympy.Matrix):
        return [[str(cell) for cell in row] for row in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _rendered(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rendered(v) for v in value]
    if isinstance(value, bool):
        return value
    return str(value)
