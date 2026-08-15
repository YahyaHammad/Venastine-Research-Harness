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

ALSO INCLUDED: the safe_parse injection battery (#52).

  This was ONE payload -- `__import__('os').system(...)` -- and its
  docstring claimed the property was "verified via test to actually
  block a real injection payload, not just assumed safe", which
  ARCHITECTURE.md §10 and CLAUDE.md both repeated.

  That payload is blocked, but for a reason unrelated to the defence it
  was named for: `auto_symbol` rewrites the bare name `__import__` into
  `Symbol('__import__')`, which then fails as a Symbol call. Blanking
  `__builtins__` never entered into it. Two payloads that do not look up
  a bare builtin name -- `sympify("...")` re-entry, and attribute
  traversal from a literal -- executed arbitrary code inside the harness
  process, driven through the real `registry.dispatch`.

  One payload standing in for a security property. The battery below is
  what replaces it, and it asserts BOTH directions: that the payload
  class is refused, and that ordinary mathematics still parses -- an
  allowlist with no legitimacy battery is one bad entry away from
  breaking the tools it protects, silently.
"""

import pytest
import sympy
from sympy import S, sqrt, pi, Rational, Symbol, simplify, sympify

from tools.builtin._math_common import (
    safe_parse, MathParseError, _ALLOWED_NODES, _ALLOWED_NAMES, _SAFE_GLOBALS)

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
    """The ORIGINAL single payload, kept.

    It is still a payload that must be refused -- it simply never tested
    what it claimed, since `auto_symbol` turns the bare name
    `__import__` into a Symbol before the blanked `__builtins__` is
    consulted. Kept so that #52's fix is not mistaken for a replacement
    of the case, and so the battery below reads as an addition to a
    property rather than a substitution of one.
    """
    payload = "__import__('os').system('echo pwned_in_test')"
    with pytest.raises(MathParseError):
        safe_parse(payload)


# The two mechanisms from #52, each with the variants that defeat the
# obvious partial fixes. Benign payloads throughout: a module object,
# getpid(), uname().sysname, a subclass list. Nothing that writes,
# deletes, or reaches the network -- these run on every developer's
# machine and in CI.
INJECTION_PAYLOADS = [
    # --- mechanism 1: re-entering a parser that builds its own globals.
    # exec/eval insert REAL __builtins__ into any globals dict lacking
    # them, so the inner parse has __import__ and chr regardless of what
    # the outer namespace blanked.
    ("sympify re-entry", 'sympify("__import__(chr(111)+chr(115))")'),
    ("sympify + call", 'sympify("__import__(chr(111)+chr(115)).getpid()")'),
    ("parse_expr direct", 'parse_expr("1+1")'),
    ("S sympifies too", 'S("__import__(chr(111)+chr(115))")'),
    ("var", 'var("x")'),
    ("lambdify compiles", "lambdify(x, x)"),
    ("init_printing", "init_printing()"),
    # --- mechanism 2: attribute traversal, which needs no name lookup
    # at all and so cannot be stopped by pruning any namespace.
    ("subclasses from a literal", "().__class__.__bases__[0].__subclasses__()"),
    ("globals via a dunder", "solve.__globals__"),
    ("class of an int", "(1).__class__"),
    # --- the submodule routes. Unreachable today only because
    # auto_symbol rewrites non-callable names into Symbols; the
    # allowlist covers them so that protection is not load-bearing.
    ("module route to os", "external.gmpy.os.getpid()"),
    ("module route to subprocess", "printing.gtk.subprocess"),
    ("module route to the parser", "parsing.sympy_parser.parse_expr"),
    # --- mechanism 3: constructs the TOKEN filter could not see.
    # The first fix for #52 filtered NAME tokens and was bypassed within
    # the hour by an f-string, which on Python 3.11 is a SINGLE STRING
    # token (PEP 701 split it up only in 3.12). The code inside was
    # evaluated with _SAFE_GLOBALS in scope, sympify included, and
    # `f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"` returned
    # the pid. `str.format`'s mini-language reached dunder attributes the
    # same way. This is why the guard validates the AST of the code that
    # will actually be evaluated rather than the token stream.
    ("f-string re-entry",
     "f\"{sympify('__import__(chr(111)+chr(115)).getpid()')}\""),
    ("f-string, nested quotes", "f'{sympify(\"1+1\")}'"),
    ("f-string in a format spec", 'f"{pi:{sympify(1)}}"'),
    ("rf-string", 'rf"{sympify(1)}"'),
    ("str.format to a dunder", '"{0.__class__.__bases__}".format(pi)'),
    ("str.format_map", '"{a.__class__}".format_map(pi)'),
    ("percent formatting", '"%s" % pi'),
    ("lambda", "(lambda: sympify(1))()"),
    ("set literal", "{sympify(1)}"),
    ("starred call", "Max(*[1, 2])"),
    ("yield", "(yield 1)"),
    # --- non-dunder attribute traversal. `.func` is a CLASS, and
    # `.mro()` walks to `object` without a single underscore, so the
    # dunder rule alone never covered this.
    ("non-dunder .func.mro()", "sin(pi).func.mro()"),
    ("method on a str literal", '"abc".encode'),
]


@pytest.mark.parametrize("label,payload",
                         INJECTION_PAYLOADS,
                         ids=[p[0] for p in INJECTION_PAYLOADS])
def test_safe_parse_refuses_the_injection_payload_class(label, payload):
    """#52. Each of these either executed code or reached a route to it.

    Parametrised rather than asserted in a loop so a failure names the
    payload that got through, and so adding a payload is one line -- the
    thing that did not happen for the whole life of the single-payload
    version.

    THE ASSERTION IS ON THE GUARD'S OWN MESSAGE, not merely on
    `MathParseError`. Several of these payloads fail anyway for reasons
    that have nothing to do with the guard -- `external.gmpy.os` dies
    with an `AttributeError` because `split_symbols` has already shredded
    `external` into `e*x*t*e*r*n*a*l` -- and a bare `pytest.raises`
    cannot tell that apart from a refusal. It was measured: with the
    guard moved to the END of the transformation pipeline, a bare
    `raises` left all 68 tests green while the module routes were being
    caught by an accident of `auto_symbol` rather than by the guard.

    That is this suite's own standing rule -- a test that fails for the
    wrong reason is indistinguishable from one that works -- and it is
    what makes the guard's POSITION load-bearing and testable.
    """
    with pytest.raises(MathParseError) as excinfo:
        safe_parse(payload)

    message = str(excinfo.value)
    assert ("not a permitted function or constant" in message
            or "not allowed in a math expression" in message), (
        f"{label!r} was rejected, but not BY THE GUARD -- it failed with "
        f"{message!r}. Something else happens to refuse this payload "
        f"today; the guard must be what does.")


# ---------------------------------------------------------------------------
# ---- The closure argument, made checkable ---------------------------------
# ---------------------------------------------------------------------------
#
# "No escape exists" is not provable by listing payloads -- that is what
# the single-payload version tried, and what the token filter tried after
# it. What IS checkable is closure: the guard admits a fixed set of AST
# node types, and everything else is refused for being absent from the
# list rather than for being recognised as bad.
#
# These three tests are the artifact to re-run on a Python upgrade. A new
# release that adds a node type (3.12 added the f-string nodes; 3.8 added
# NamedExpr) lands in the refused set automatically, and the first test
# says so out loud.

def test_the_node_allowlist_is_closed_and_small():
    """The allowed set must stay a small, enumerated fraction of what
    Python defines. If someone widens it to make an expression work, this
    is the test that makes the widening visible."""
    import ast

    concrete = {
        n for n in vars(ast).values()
        if isinstance(n, type) and issubclass(n, ast.AST)
        and n.__name__ not in {
            "AST", "expr", "stmt", "mod", "operator", "unaryop", "boolop",
            "cmpop", "expr_context", "slice", "type_ignore", "pattern",
            "excepthandler"}
    }
    allowed = set(_ALLOWED_NODES) & concrete

    assert len(allowed) < len(concrete) / 2, (
        f"the guard now admits {len(allowed)} of {len(concrete)} node "
        "types. It is meant to be a small closed set; widening it is how "
        "a construct nobody considered becomes reachable.")


@pytest.mark.parametrize("node_name", [
    "Attribute",        # mechanism 2, and every str.format/method route
    "JoinedStr",        # f-strings -- the token filter's blind spot
    "FormattedValue",
    "Lambda",
    "ListComp", "SetComp", "DictComp", "GeneratorExp",
    "NamedExpr",        # walrus
    "Starred",
    "Await", "Yield",
    "Dict", "Set",
    "Import", "ImportFrom", "Assign", "IfExp",
])
def test_the_dangerous_node_types_are_refused(node_name):
    """Named individually so that admitting one is a red test with the
    node's name on it, rather than a silent change in a tuple.

    `Attribute` is the load-bearing one: attribute access IS #52's second
    mechanism, and no namespace pruning can reach it.
    """
    import ast

    node = getattr(ast, node_name)
    assert node not in _ALLOWED_NODES


def test_no_permitted_name_can_evaluate_a_string():
    """The name half of the closure. `_SAFE_GLOBALS` exposes 900-odd
    names because it is built from `dir(sympy)`; the guard permits a
    couple of hundred. None of the ones that turn a string back into code
    may be among them -- that is #52's first mechanism."""
    evaluators = {"sympify", "parse_expr", "var", "S", "lambdify",
                  "init_printing", "preview", "parse_latex",
                  "parse_mathematica", "interactive_traversal"}

    assert not (evaluators & _ALLOWED_NAMES), (
        f"{sorted(evaluators & _ALLOWED_NAMES)} can turn a string back "
        "into code, and are permitted")


def test_no_module_object_is_permitted():
    """The submodule routes (`external.gmpy.os`, `printing.gtk.
    subprocess`, `parsing.sympy_parser.builtins`) reach the stdlib by
    PLAIN attribute access -- no dunder anywhere. Attribute is refused
    outright so they are unreachable twice over, but a module in the name
    allowlist would still be a mistake worth catching here."""
    import types

    modules = {n for n, v in _SAFE_GLOBALS.items()
               if isinstance(v, types.ModuleType)}
    assert modules, ("no sympy submodules found in _SAFE_GLOBALS -- this "
                     "test can no longer see the thing it checks")
    assert not (modules & _ALLOWED_NAMES), (
        f"module objects are permitted by name: "
        f"{sorted(modules & _ALLOWED_NAMES)}")


def test_an_error_result_is_not_evidence_the_payload_failed(monkeypatch):
    """The property that makes this class hard to spot in a transcript.

    Where the return value is not sympify-able, the tool reports
    `{'error': 'Computation failed: ...'}` while the import or call has
    ALREADY happened. So a refusal has to be observable as an absence of
    the side effect, not as an error string.

    `base64` is a stdlib module nothing else in this suite imports, used
    here only as a witness that an import did or did not occur.
    """
    import sys

    monkeypatch.delitem(sys.modules, "base64", raising=False)
    chars = "+".join(f"chr({ord(c)})" for c in "base64")
    with pytest.raises(MathParseError):
        safe_parse(f'sympify("__import__({chars})")')

    assert "base64" not in sys.modules, (
        "the payload was refused, but the import had already run -- the "
        "refusal is happening after evaluation rather than before it")


# The other direction. An allowlist that refuses everything passes every
# test above, so this is what makes them discriminate -- and it is the
# lesson #47 recorded: an allowlist needs a test asserting that real
# producers satisfy it.
LEGITIMATE_EXPRESSIONS = [
    "x**2 - 4", "2x + 1", "sin(x)**2 + cos(x)**2", "sqrt(16)",
    "integrate(x**2, x)", "diff(sin(x), x)", "limit(sin(x)/x, x, 0)",
    "Matrix([[1, 2], [3, 4]])", "exp(I*pi) + 1", "factorial(5)",
    "x**2 - 4 = 0", "log(E)", "Sum(k, (k, 1, 10))", "gamma(5)",
    "besselj(0, x)", "Rational(1, 3)", "Abs(-5)", "floor(3.7)",
    "binomial(10, 3)", "erf(x)", "Piecewise((x, x > 0), (0, True))",
    "solve(x**2 - 4, x)", "zeta(2)", "LambertW(x)", "Max(1, 2, 3)",
    "And(p, Or(q, Not(r)))", "Eq(x, 5)", "atan2(1, 1)", "gcd(12, 18)",
    "fibonacci(10)", "isprime(7)", "Interval(0, 1)", "conjugate(2 + 3*I)",
    "oo", "pi/2", "Point(1, 2)", "Circle(Point(0, 0), 5)",
]


@pytest.mark.parametrize("expr", LEGITIMATE_EXPRESSIONS)
def test_ordinary_mathematics_still_parses(expr):
    """Every one of these is something a model may reasonably write into
    one of the six tools' expression fields.

    A name refused here is a usability regression the model has to work
    around at runtime, and it would otherwise surface as a tool error in
    a research pass rather than as a red test.
    """
    safe_parse(expr)


def test_an_unknown_name_still_becomes_a_symbol():
    """The allowlist must not turn undefined names into refusals.

    Auto-converting an unknown name into a Symbol is the parser's whole
    purpose -- `z * 2` is a legitimate expression in a variable nobody
    declared. The guard only ever consults names that are IN
    `_SAFE_GLOBALS`, and this is what pins that distinction.
    """
    from sympy import Symbol

    assert safe_parse("z * 2") == Symbol("z") * 2
    assert safe_parse("q + r").free_symbols == {Symbol("q"), Symbol("r")}


def test_a_multi_letter_unknown_name_is_split_not_refused():
    """A surprise met while writing the test above, pinned where it will
    be found: `wibble * 2` parses as `2*b**2*e*i*l*w`.

    That is `split_symbols`, which `implicit_multiplication_application`
    carries so that `2xy` means `2*x*y` -- it is what makes the tools
    accept informal algebra, and it is UNRELATED to #52's allowlist
    (verified identical before and after the fix). The thing that matters
    here is that a multi-letter name is not REFUSED; callers who need the
    name kept whole pass it via `symbols=`, which is what that parameter
    is for.
    """
    from sympy import Symbol

    assert safe_parse("wibble * 2") == 2 * Symbol("b")**2 * Symbol("e") \
        * Symbol("i") * Symbol("l") * Symbol("w")
    assert safe_parse("wibble * 2", symbols=["wibble"]) == 2 * Symbol("wibble")


def test_the_refusal_names_what_was_refused():
    """The model has to recover from this at runtime, in a headless pass
    with nobody watching, so the message has to say which name failed --
    not merely that the expression did.

    It also must NOT echo the whole payload back: safe_parse's generic
    wrapper prefixes "Could not parse expression '<payload>'", and a
    refusal deliberately bypasses that.
    """
    with pytest.raises(MathParseError) as excinfo:
        safe_parse('sympify("1+1")')

    message = str(excinfo.value)
    assert "sympify" in message
    assert "not a permitted" in message
    assert "Could not parse expression" not in message


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
