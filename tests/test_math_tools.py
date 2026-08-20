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
  ARCHITECTURE.md §10 and AGENTS.md both repeated.

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

import sys

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
    # --- mechanism 4: a STRING LITERAL reaching a function that
    # sympifies its own argument. Found by auditing the allowlist's
    # source for calls to eval/exec/sympify rather than by guessing:
    # seven of the permitted names do it. Handing one a string re-enters
    # the parser with SymPy's default globals -- real builtins included.
    #
    # The leading space is the whole trick. An earlier version of the
    # guard tripped only on a string that STARTED OR ENDED with a
    # dunder, so one space walked past it -- the same category of
    # mistake as `chr(111)+chr(115)` defeating a search for "os".
    ("factor sympifies its arg",
     'factor(" __import__(chr(111)+chr(115)).getpid()")'),
    ("cancel sympifies its arg",
     'cancel(" __import__(chr(111)+chr(115)).getpid()")'),
    ("together sympifies its arg",
     'together(" __import__(chr(111)+chr(115)).getpid()")'),
    ("apart sympifies its arg",
     'apart(" __import__(chr(111)+chr(115)).getpid()")'),
    ("roots sympifies its arg",
     'roots(" __import__(chr(111)+chr(115)).getpid()")'),
    ("degree sympifies its arg",
     'degree(" __import__(chr(111)+chr(115)).getpid()")'),
    ("a bare string operand", '"%s" % pi'),
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


def test_string_literals_are_licensed_only_where_the_parser_emits_them():
    """The string rule, stated as the closure it is.

    `stringify_expr` gives a string argument to exactly two calls --
    `Symbol('x')` for every free variable and `Float('1.5')` for every
    non-integer literal. Everything else is refused, which is what closes
    mechanism 4 without needing to know WHICH functions sympify their
    arguments. Widening this set means arguing that the new constructor
    does not sympify what it is handed.
    """
    from tools.builtin._math_common import _STRING_ARG_CONSTRUCTORS

    assert _STRING_ARG_CONSTRUCTORS == {"Symbol", "Float"}

    # Both must genuinely still work, or every expression breaks.
    safe_parse("x + 1")          # -> Symbol('x') + Integer(1)
    safe_parse("1.5*y")          # -> Float('1.5') * Symbol('y')


def test_a_symbol_name_may_be_an_arbitrary_string_and_is_inert():
    """`Symbol` is licensed, so `Symbol(" anything ")` parses. Recorded
    rather than treated as a leak: a Symbol's name is a label, it is
    never evaluated, and refusing odd names would mean validating
    identifiers for no security gain.

    Pinned so that if `Symbol` ever gains string-evaluating behaviour,
    the licensing decision is revisited deliberately.
    """
    from sympy import Symbol

    out = safe_parse('Symbol(" __import__(chr(111)) ")')
    assert isinstance(out, Symbol)
    assert out.name == " __import__(chr(111)) "


def test_no_allowlisted_name_evaluates_a_string_it_can_be_handed():
    """The audit, as a test.

    Seven allowlisted functions call `sympify()` internally. That is fine
    -- their arguments are already SymPy objects by the time the parser
    calls them -- and it is ONLY fine because no string can reach them.
    This asserts the two halves together: the sympifying names are still
    permitted (so this test is not vacuous), and a string cannot be
    passed to one.
    """
    sympifying = {"factor", "cancel", "together", "apart", "roots",
                  "degree", "nsolve"}
    assert sympifying <= _ALLOWED_NAMES, (
        "these are permitted names; if they were removed this test stops "
        "testing anything")

    for name in sorted(sympifying):
        with pytest.raises(MathParseError) as excinfo:
            safe_parse(f'{name}(" 1+1")')
        assert "string literals are not allowed" in str(excinfo.value), (
            f"a string literal reached {name}(), which sympifies it")


# ===========================================================================
# ---- (1) The output backstop ----------------------------------------------
# ===========================================================================
#
# Every other guard is an allowlist over the INPUT. This is the only check
# on the OUTPUT, and it is deliberately a DENY of the escape signature
# rather than an allowlist of return types: its job is to catch an escape
# nobody anticipated, at the moment it succeeds. An allowlist of return
# types can only catch escapes whose shape was already imagined -- which
# is the failure mode this whole issue is a record of.
#
# The signature: a math expression evaluates to a VALUE. It never
# evaluates to a module, a plain function, or a class from outside SymPy.
# Those are precisely what #52's escapes produced or reached for --
# `<module 'os'>`, `__subclasses__`, a bound method's `__globals__`.

def test_the_backstop_refuses_a_module():
    from tools.builtin._math_common import _reject_escaped_value
    import os

    with pytest.raises(MathParseError, match="module"):
        _reject_escaped_value(os)


def test_the_backstop_refuses_a_non_sympy_class():
    from tools.builtin._math_common import _reject_escaped_value

    with pytest.raises(MathParseError, match="not a SymPy type"):
        _reject_escaped_value(type)
    with pytest.raises(MathParseError, match="not a SymPy type"):
        _reject_escaped_value(dict)


def test_the_backstop_refuses_functions_and_methods():
    from tools.builtin._math_common import _reject_escaped_value

    with pytest.raises(MathParseError, match="function or method"):
        _reject_escaped_value(len)
    with pytest.raises(MathParseError, match="function or method"):
        _reject_escaped_value("".join)


def test_the_backstop_looks_inside_containers():
    """`solve()` returns a list and `roots()` returns a dict, so an
    escaped object could arrive nested rather than at the top level. It
    would be a strange kind of guard that checked only the outermost
    value of a function whose normal return type is a container."""
    from tools.builtin._math_common import _reject_escaped_value
    import os

    for container in ([os], (os,), {os}, {"k": os}, {os: 1}, [[[os]]]):
        with pytest.raises(MathParseError):
            _reject_escaped_value(container)


def test_the_backstop_allows_every_legitimate_return_shape():
    """The discriminating half. A backstop that refuses everything passes
    all four tests above.

    These are the concrete top-level types the legitimacy battery
    actually produces -- including the three that are NOT `Basic`:
    a mutable Matrix, a `list` from `solve`, a `dict` from `roots`, and a
    plain `bool` from `isprime`.
    """
    from tools.builtin._math_common import _reject_escaped_value

    for expr in ("x**2 - 4", "Matrix([[1, 2], [3, 4]])", "solve(x**2 - 4, x)",
                 "roots(x**2 - 1)", "isprime(7)", "divisors(12)", "oo",
                 "Interval(0, 1)", "1.5", "Point(1, 2)"):
        _reject_escaped_value(safe_parse(expr))   # must not raise


def test_a_sympy_class_is_a_legitimate_value():
    """`sin` is a `FunctionClass` and `Function('f')` is a class too --
    both subclass `Basic`, which is exactly what separates them from
    `type` or `os`. Refusing all classes would be simpler and would
    break both."""
    from tools.builtin._math_common import _reject_escaped_value

    _reject_escaped_value(safe_parse("sin"))
    # Constructed directly, not parsed: see the test below for why
    # `Function('f')` is not reachable through safe_parse.
    _reject_escaped_value(sympy.Function("f"))


def test_function_with_a_string_name_is_refused_by_the_string_rule():
    """A deliberate, recorded cost of licensing only `Symbol` and
    `Float`.

    `Function('f')` is a legitimate SymPy expression and is refused,
    because its string argument sits in an unlicensed position. Adding
    `Function` to `_STRING_ARG_CONSTRUCTORS` would be safe on the pinned
    version -- the blackbox probe below shows it is not among the 117
    callables that evaluate a string -- but it is not needed by any of
    the six tools, and every licensed position is one more thing that has
    to be re-argued when SymPy changes.

    Pinned so that the day someone needs it, the trade is visible rather
    than rediscovered.
    """
    with pytest.raises(MathParseError, match="string literals"):
        safe_parse("Function('f')")


def test_the_backstop_is_actually_wired_into_safe_parse(monkeypatch):
    """Guards against the check existing but never being called -- which
    is the failure mode #58 catalogued for four other security guards in
    this package, each of which could be deleted with the suite green."""
    import tools.builtin._math_common as mc

    called = []
    monkeypatch.setattr(mc, "_reject_escaped_value",
                        lambda v, _depth=0: called.append(v))
    mc.safe_parse("x + 1")

    assert called, "safe_parse returned without consulting the backstop"


# ===========================================================================
# ---- (2) The blackbox audit of the allowlist ------------------------------
# ===========================================================================
#
# The source audit that found mechanism 4 could read only 15 of the 229
# resolvable allowlisted names -- the other 214 are C extensions with no
# retrievable source. This is the blackbox version, which covers all of
# them: hand each callable a string and see whether it evaluates it.
#
# The measured answer is 117 of 213, not the 7 the source audit found. So
# the string-licensing rule is not a nicety; it is what stands between the
# model and a hundred-odd evaluators.

_WITNESS_MODULE = "colorsys"      # stdlib, harmless, not used by this project
_EVAL_PROBE = (" __import__("
               + "+".join(f"chr({ord(c)})" for c in _WITNESS_MODULE) + ") ")


def _evaluates_its_string_argument(fn) -> bool:
    """True if `fn(payload)` sympified the payload, observed by whether a
    module got imported as a side effect.

    A witness import is used rather than a return value because most of
    these raise AFTER sympifying -- which is the whole reason an error
    result is not evidence that a payload failed.
    """
    import signal
    import warnings

    sys.modules.pop(_WITNESS_MODULE, None)
    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(2)
    try:
        # Handing 213 SymPy callables a nonsense argument provokes
        # deprecation warnings that say nothing about this test.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fn(_EVAL_PROBE)
    except BaseException:
        pass
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    imported = _WITNESS_MODULE in sys.modules
    sys.modules.pop(_WITNESS_MODULE, None)
    return imported


class _ProbeTimeout(Exception):
    pass


def _raise_timeout(_signum, _frame):
    raise _ProbeTimeout()


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"),
                    reason="probe needs SIGALRM to bound a hanging callable")
def test_the_licensed_string_constructors_do_not_evaluate_their_argument():
    """THE OBLIGATION the licensing decision rests on.

    `Symbol` and `Float` are the only two calls permitted to receive a
    string literal. If either sympified what it was handed, mechanism 4
    would be open through the very positions that were licensed to keep
    ordinary algebra working.
    """
    from tools.builtin._math_common import _STRING_ARG_CONSTRUCTORS

    for name in sorted(_STRING_ARG_CONSTRUCTORS):
        fn = _SAFE_GLOBALS[name]
        assert not _evaluates_its_string_argument(fn), (
            f"{name}() evaluates a string argument, and it is licensed to "
            f"receive one -- mechanism 4 is open through it")


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"),
                    reason="probe needs SIGALRM to bound a hanging callable")
def test_the_probe_can_detect_an_evaluator():
    """Guards the guard. The test above asserts a NEGATIVE, so it passes
    perfectly if the probe silently stopped working -- if the witness
    module became permanently imported, or the payload stopped being
    valid.

    `factor` is one of the seven the source audit caught red-handed, so
    it is the control that proves the probe still sees what it is for.
    """
    assert _evaluates_its_string_argument(_SAFE_GLOBALS["factor"]), (
        "the probe no longer detects a known string-evaluating callable, "
        "so the test above is asserting nothing")


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"),
                    reason="probe needs SIGALRM to bound a hanging callable")
def test_most_of_the_allowlist_evaluates_strings_which_is_why_they_cannot_get_one():
    """The measurement, kept as a test so the number cannot quietly drift.

    117 of 213 on the pinned SymPy. The exact figure is not the point and
    is not asserted; what is asserted is that it is a LARGE fraction --
    because the argument "the allowlist is safe because its members are
    just maths" is false, and this is the evidence. The string rule is
    load-bearing, not defence in depth.
    """
    callables = [(n, _SAFE_GLOBALS[n]) for n in sorted(_ALLOWED_NAMES)
                 if callable(_SAFE_GLOBALS.get(n))]
    assert len(callables) > 150, "the allowlist shrank; re-check this test"

    evaluators = [n for n, fn in callables
                  if _evaluates_its_string_argument(fn)]

    assert len(evaluators) > 50, (
        f"only {len(evaluators)} allowlisted callables evaluate a string "
        "argument. If SymPy really stopped sympifying arguments this is "
        "good news, but verify it rather than relaxing the string rule.")

    from tools.builtin._math_common import _STRING_ARG_CONSTRUCTORS
    assert not (set(evaluators) & _STRING_ARG_CONSTRUCTORS), (
        f"{sorted(set(evaluators) & _STRING_ARG_CONSTRUCTORS)} both "
        "evaluate strings AND are licensed to receive them")


# ===========================================================================
# ---- §31 (H5, H6): two shape defects, found while checking whether --------
# ---- oversized integers were exploitable ----------------------------------
# ===========================================================================
#
# Neither is a SIZE problem, which is why neither is fixed by a bound on a
# parameter. An `le` on `exponent` would have caught neither, and H4
# declines to add one anyway: cost in `combinations` is joint in n and r,
# so no per-parameter ceiling expresses it.


class TestModularExponentIsModular:
    """H5. `modulus` is Optional and `pow(b, e, None)` is PLAIN
    exponentiation, so omitting one field turned this into a different
    operation with a different complexity class -- measured at 0.000s with
    a modulus and 3.3s / 125 MB without one, for the same exponent."""

    def test_a_missing_modulus_is_refused_rather_than_computed(self):
        result = discrete_math.run({"operation": "modular_exponent",
                                    "base": 2, "exponent": 1000000000})
        assert "error" in result
        assert "modulus" in result["error"]

    def test_the_refusal_is_immediate_not_a_timeout(self):
        """The point of refusing at the boundary rather than leaning on
        H2's clock: the same call would otherwise cost a full budget and
        125 MB of allocation before anything was said."""
        import time
        started = time.time()
        discrete_math.run({"operation": "modular_exponent",
                           "base": 2, "exponent": 1000000000})
        assert time.time() - started < 1.0

    def test_the_same_call_with_a_modulus_is_answered(self):
        """The control. Modular exponentiation of a huge exponent is cheap
        -- that is what the operation is FOR -- so the fix must not touch
        it."""
        result = discrete_math.run({"operation": "modular_exponent",
                                    "base": 2, "exponent": 1000000000,
                                    "modulus": 1000003})
        assert result == {"result": "623034",
                          "operation": "modular_exponent"}


class TestAResultTooLargeToPrintIsTheToolsOwnError:
    """H6. `str(result)` sat OUTSIDE run()'s try, so a correct answer that
    merely could not be printed left the handler as a bare ValueError, was
    caught by dispatch's containment, and was logged with a full traceback
    as though it were a bug in the harness."""

    def test_an_unprintable_result_is_an_error_dict(self):
        result = discrete_math.run({"operation": "factorial", "n": 100000})
        assert "error" in result

    def test_the_message_says_what_to_do_about_it(self):
        """dispatch's generic wrapper -- "discrete_math failed: Exceeds the
        limit (4300 digits)..." -- describes CPython to a model that can
        only change its own inputs."""
        result = discrete_math.run({"operation": "factorial", "n": 100000})
        assert "smaller" in result["error"].lower()

    def test_it_does_not_escape_the_handler(self):
        """The property, stated directly: nothing raises out of run()."""
        for params in ({"operation": "factorial", "n": 100000},
                       {"operation": "combinations", "n": 100000,
                        "r": 50000}):
            assert isinstance(discrete_math.run(params), dict)

    def test_a_dict_result_still_carries_its_values_untouched(self):
        """The guard wraps the serialisation; it must not change what is
        serialised. prime_factors returns {prime: exponent} with INTEGER
        exponents, and a str() applied one level too far would rewrite
        every one of them -- which is what the first draft of this fix
        did."""
        result = discrete_math.run({"operation": "prime_factors", "n": 360})
        assert result == {"result": {"2": 3, "3": 2, "5": 1},
                          "operation": "prime_factors"}

    def test_an_ordinary_factorial_is_unaffected(self):
        result = discrete_math.run({"operation": "factorial", "n": 20})
        assert result == {"result": "2432902008176640000",
                          "operation": "factorial"}
