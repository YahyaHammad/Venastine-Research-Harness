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
from sympy import Symbol, pi, simplify, sympify

import config
import tools.builtin.discrete_math as discrete_math
import tools.builtin.geometry as geometry
import tools.builtin.linear_algebra as linear_algebra
import tools.builtin.logic as logic
import tools.builtin.probability_stats as probability_stats
import tools.builtin.symbolic_math as symbolic_math
from tools.builtin._math_common import (
    _ALLOWED_NAMES,
    _ALLOWED_NODES,
    _SAFE_GLOBALS,
    MathParseError,
    MathResultTooLarge,
    _reject_escaped_value,
    safe_parse,
    serialize,
)

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

    THAT LAST SENTENCE WAS NOT TRUE OF THE TOOLS UNTIL BATCH 130's
    successor (S06-01). `safe_parse` has always taken `symbols=`, but
    `linear_algebra` and `geometry` had no parameter through which a caller
    could reach it and `symbolic_math` passed it on two of its six parses --
    so this test asserted the behaviour and cited a remedy that could not
    be applied, which is the vacuity shape turned inside out: not an
    assertion that cannot discriminate, but a remedy recorded as though it
    had been available. See
    `TestASplitSymbolNameIsReportedRatherThanAnswered` at the end of this
    file for the reachability guard.
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
    import os

    from tools.builtin._math_common import _reject_escaped_value

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
    import os

    from tools.builtin._math_common import _reject_escaped_value

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


# ===========================================================================
# ---- S06-01: a split name is reported, and the remedy is reachable --------
# ===========================================================================

class TestASplitSymbolNameIsReportedRatherThanAnswered:
    """`_TRANSFORMATIONS` carries `split_symbols`, so a multi-character name
    absent from `local_dict` becomes a PRODUCT of single letters before
    anything is computed: `determinant([[mass,0],[0,mass]])` answered
    `a**2*m**2*s**4` as a successful `{"result": ...}`, with no marker
    distinguishing it from a correct answer (S06-01, severity S1).

    The split itself is deliberate and stays -- it is what makes the tools
    accept informal algebra, and
    `test_a_multi_letter_unknown_name_is_split_not_refused` above pins it.
    What changed is that the remedy that test's docstring names is now
    REACHABLE from every parsing tool, and that an undeclared split is said
    out loud instead of being returned as though nothing happened.
    """

    def test_an_undeclared_name_is_answered_with_a_warning_naming_it(self):
        out = linear_algebra.run({"operation": "determinant",
                                  "matrix_a": [["mass", "0"], ["0", "mass"]]})
        # The answer still comes back -- forgiving, per the split's own
        # rationale -- but it no longer comes back silent.
        assert out["result"] == "a**2*m**2*s**4"
        assert len(out["warnings"]) == 1
        assert "mass" in out["warnings"][0]
        assert "symbols=['mass']" in out["warnings"][0]

    def test_a_declared_name_survives_and_warns_about_nothing(self):
        out = linear_algebra.run({"operation": "determinant",
                                  "matrix_a": [["mass", "0"], ["0", "mass"]],
                                  "symbols": ["mass"]})
        assert out["result"] == "mass**2"
        assert "warnings" not in out

    def test_the_other_two_parsing_tools_reach_the_same_remedy(self):
        """`geometry` and `symbolic_math` are the two the finding is about:
        neither could pass `symbols=` at all, and `symbolic_math` protected
        only the ONE name in `variable` while shredding any other name in
        the same expression."""
        bare = geometry.run({"operation": "distance",
                             "points": [["mass", "0"], ["0", "0"]]})
        assert bare["warnings"] and "mass" in bare["warnings"][0]
        fixed = geometry.run({"operation": "distance",
                              "points": [["mass", "0"], ["0", "0"]],
                              "symbols": ["mass"]})
        assert fixed["result"] == "sqrt(mass**2)"
        assert "warnings" not in fixed

        # The clearest statement of the shape: the one name the schema let
        # you protect survived and the other one was shredded.
        bare = symbolic_math.run({"operation": "integrate",
                                  "expression": "force*distance",
                                  "variable": "distance"})
        assert bare["result"] == "c*distance**2*e*f*o*r/2"
        assert bare["warnings"] and "force" in bare["warnings"][0]
        fixed = symbolic_math.run({"operation": "integrate",
                                   "expression": "force*distance",
                                   "variable": "distance",
                                   "symbols": ["force"]})
        assert fixed["result"] == "distance**2*force/2"
        assert "warnings" not in fixed

    @pytest.mark.parametrize("entry,expected", [
        ("T", "T**2"),               # one character cannot be split
        ("mass_1", "mass_1**2"),     # an underscore suppresses the split
        ("alpha", "alpha**2"),       # a Greek-letter name does too
        ("E", "exp(2)"),             # a SymPy binding the caller may have meant
    ])
    def test_a_name_that_was_not_split_stays_silent(self, entry, expected):
        """The warning has to be specific or it is noise on every call.

        `E` is in the list deliberately: it is Euler's number rather than a
        free symbol, so `E**2` IS `exp(2)` and deciding that the caller
        meant a symbol is not this layer's business -- `symbols=["E"]` is
        how a caller overrides it. The finding put the constant bindings
        explicitly out of scope.
        """
        out = linear_algebra.run({"operation": "determinant",
                                  "matrix_a": [[entry, "0"], ["0", entry]]})
        assert out["result"] == expected
        assert "warnings" not in out

    def test_a_name_that_cancels_out_of_the_result_is_judged_on_what_it_was(self):
        """The detector asks "is this name among the result's free symbols",
        which is the wrong question on its own for a name that CANCELS.

        `mass*0` evaluates to 0, whose free symbols are empty, so a
        declared `mass` and a single-letter `y` both look identical to a
        split name unless the declaration and the length are checked first.
        Those two exclusions exist for this case and nothing else, so
        without this test they could be deleted silently -- and the first
        version of this test could not see it, because it asked
        `linear_algebra` where `safe_parse` runs once per CELL and a
        declared name therefore always survives its own parse. The mutation
        pass caught that; the undeclared half below is the control that
        proves this expression can still produce a warning at all.
        """
        declared = symbolic_math.run({"operation": "evaluate",
                                      "expression": "mass*0",
                                      "symbols": ["mass"]})
        assert declared["result"] == "0"
        assert "warnings" not in declared

        undeclared = symbolic_math.run({"operation": "evaluate",
                                        "expression": "mass*0"})
        assert undeclared["warnings"] and "mass" in undeclared["warnings"][0]

        single = symbolic_math.run({"operation": "evaluate",
                                    "expression": "y*0"})
        assert "warnings" not in single

    def test_a_function_call_and_a_float_literal_are_not_names(self):
        """Two false positives the detector has to not have: `sin` is an
        allowlisted binding, and the `e5` inside `1e5` is not a name the
        caller wrote at all."""
        assert "warnings" not in symbolic_math.run(
            {"operation": "differentiate", "expression": "sin(x)"})
        assert "warnings" not in symbolic_math.run(
            {"operation": "evaluate", "expression": "1e5"})

    def test_every_parsing_tool_can_declare_a_symbol(self):
        """The structural guard, and the one that would have caught this
        class rather than this instance.

        The defect was not that names split -- it was that the remedy the
        pinned test NAMES was unreachable from two of the four tools, and
        the test asserted the behaviour without asking whether the remedy
        it cited could be applied. A tool that grows a parsed field and no
        way to declare its symbols reintroduces exactly that, so this
        asserts the capability rather than any one answer.
        """
        for module, field in ((linear_algebra, "symbols"),
                              (geometry, "symbols"),
                              (symbolic_math, "symbols"),
                              (logic, "variables")):
            params = module.TOOL_SCHEMA["input_schema"]["properties"]
            assert field in params, (
                "%s advertises no way to declare a multi-character symbol, "
                "so split_symbols can rewrite a model-supplied name with no "
                "remedy the model can reach" % module.TOOL_SCHEMA["name"])


# ===========================================================================
# ---- S06-02 / S06-03: a math result is bounded, and bounded at the --------
# ---- producer. Batch 143. -------------------------------------------------
# ===========================================================================
#
# Two bounds, deliberately not one, and the split is the design:
#
#   * `_math_common.serialize` caps the RENDERED result at
#     `config.MAX_READ_CHARS`. It is the only place three of the six tools
#     turn a SymPy object into something returnable, so one cap there
#     covers every operation they have, including ones nobody enumerated.
#   * `logic` refuses a truth table on its COLUMN COUNT, before the rows
#     exist, because 2**n is the one math output whose size is known from
#     the input. H5's move.
#
# What was there before was `tools/isolation.MAX_CHILD_OUTPUT_BYTES`, a
# 4 MB transport limit -- so a 14-symbol truth table (27 characters of
# input, 1.36s) returned 3,440,679 characters, 786,441 proxy tokens, 49x
# `compaction_min_headroom_tokens`.


class TestAMathResultIsBoundedAtTheProducer:
    """S06-02, the output cap. `config.MAX_READ_CHARS` rather than a new
    constant: `config.yaml` already calls it "the largest a single bounded
    read can return" and sizes `compaction_min_headroom_tokens` from it, so
    a second number would be a second premise for one piece of
    arithmetic."""

    def test_the_documented_worst_case_is_refused_with_both_numbers(self):
        """`series order=1000` is the call CONFIG_ARCHITECTURE names as the
        slowest legitimate one. Its OUTPUT was never measured: 1,185,090
        characters, 296,272 proxy tokens, 23x the premise."""
        out = symbolic_math.run({"operation": "series",
                                 "expression": "exp(x)", "order": 1000})
        assert "error" in out
        assert "series" in out["error"]
        assert str(config.MAX_READ_CHARS) in out["error"]
        assert "smaller" in out["error"].lower()

    def test_an_ordinary_series_is_untouched(self):
        """The discriminating half. Measured: `order=200` is 35,088
        characters and `order=100` is 7,748, so the cap has to leave both
        alone or it is not a bound, it is a feature removal."""
        for order, expected_substring in ((10, "x**9"), (100, "x**99")):
            out = symbolic_math.run({"operation": "series",
                                     "expression": "exp(x)", "order": order})
            assert "error" not in out
            assert expected_substring in out["result"]

    def test_the_whole_legitimacy_battery_stays_under_the_cap(self):
        """Measured before the cap was chosen rather than after: every one
        of these is under 3,000 characters except the two series rows, and
        the largest in the battery (`expand((x+y+z)**40)`) is 27,340."""
        battery = [
            (symbolic_math, {"operation": "simplify",
                             "expression": "sin(x)**2 + cos(x)**2"}),
            (symbolic_math, {"operation": "solve",
                             "expression": "x**2 - 4 = 0"}),
            (symbolic_math, {"operation": "expand",
                             "expression": "(x + y + z)**40"}),
            (linear_algebra, {"operation": "inverse",
                              "matrix_a": [["a", "b", "c"],
                                           ["d", "e", "f"],
                                           ["g", "h", "i"]]}),
            (linear_algebra, {"operation": "eigenvectors",
                              "matrix_a": [["1", "2"], ["3", "4"]]}),
            (geometry, {"operation": "triangle_properties",
                        "points": [["0", "0"], ["4", "0"], ["0", "3"]]}),
            (discrete_math, {"operation": "factorial", "n": 1000}),
            (probability_stats, {"operation": "describe",
                                 "data": [1.0, 2.0, 3.0, 4.0, 5.0]}),
        ]
        for module, params in battery:
            out = module.run(params)
            assert "error" not in out, (
                "%s %s is in the legitimacy battery and the cap refused it: "
                "%s" % (module.TOOL_SCHEMA["name"], params["operation"],
                        out.get("error")))

    def test_the_size_is_measured_on_the_finished_structure(self):
        """The cap is applied ONCE, to the whole rendered result, and this
        is the test that says so.

        A cap applied per recursion level would pass this: 120 rows of 120
        one-character cells is 57,600 characters of JSON while every inner
        list is 480. That is why `_rendered` exists as a separate function
        -- `serialize` measures what `_rendered` returns rather than
        checking a length on the way down.
        """
        wide = [["1"] * 120 for _ in range(120)]
        with pytest.raises(MathResultTooLarge):
            serialize(wide)

        # The control: the same shape, small enough, comes back whole.
        assert serialize([["1"] * 2 for _ in range(2)]) == \
            [["1", "1"], ["1", "1"]]

    def test_the_ceiling_is_the_config_value_and_is_read_per_call(self):
        """The strongest control there is for "which number is this".

        Lowering `config.MAX_READ_CHARS` has to move the boundary, and
        raising it has to let the refused call through. A hardcoded 50,000
        that merely happens to equal today's config passes every other test
        in this class and fails this one.
        """
        tiny = {"operation": "series", "expression": "exp(x)", "order": 10}
        assert "error" not in symbolic_math.run(tiny)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(config, "MAX_READ_CHARS", 50)
            out = symbolic_math.run(tiny)
            assert "error" in out
            assert "ceiling of 50" in out["error"]

        # And the other direction, which is the half a hardcoded constant
        # cannot fake: a call that IS refused today comes back when the
        # ceiling is raised above it. `expand((x+y+z)**60)` is 74,723
        # characters and 0.88s -- the same property `series order=1000`
        # would show for 10s.
        #
        # That exact figure is a function of `sympy==1.13.3`, which
        # requirements.txt pins exactly, so it is stable by construction
        # rather than by luck. If the pin moves and this goes red, what it
        # is reporting is that the tools' output sizes moved, which is the
        # thing a bound sized from measurement wants to be told.
        over = {"operation": "expand", "expression": "(x + y + z)**60"}
        assert "74723 characters" in symbolic_math.run(over)["error"]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(config, "MAX_READ_CHARS", 5_000_000)
            assert "error" not in symbolic_math.run(over)

    def test_the_refusal_is_the_tools_own_error_not_an_exception(self):
        """The property H6 states, for the bound rather than the printer:
        nothing raises out of `run()`."""
        for module, params in (
                (symbolic_math, {"operation": "expand",
                                 "expression": "(x + y + z)**60"}),
                (symbolic_math, {"operation": "simplify",
                                 "expression": "factorial(3000)"}),
                (linear_algebra, {"operation": "determinant",
                                  "matrix_a": [["factorial(3000)", "0"],
                                               ["0", "1"]]}),
                (geometry, {"operation": "distance",
                            "points": [["factorial(3000)", "0"],
                                       ["0", "0"]]})):
            out = module.run(params)
            assert isinstance(out, dict) and "error" in out


class TestTheThreeToolsSharingSerializeCatchItsFailure:
    """S06-03. H6 is stated generally in ROADMAP_v2 and was implemented in
    one of four places: `discrete_math` serialises by hand and caught it,
    while `symbolic_math`, `linear_algebra` and `geometry` called
    `serialize` in the `return` statement, OUTSIDE every `try`.

    So a correct answer carrying an integer above CPython's 4,300-digit
    `int -> str` cap left `run()` as a bare `ValueError`, came back as
    `exit 1` ("did not return a usable result") -- the branch
    `test_a_crashed_child_is_NOT_reported_as_a_limit` reserves for a
    harness BUG -- and handed the model 400 characters of traceback naming
    the install path under `C:\\Users\\<username>\\AppData\\Roaming`.
    """

    def test_symbolic_math_reports_its_own_unprintable_result(self):
        """`simplify factorial(3000)` rather than `series order=4000`: both
        reach `str()` of a 9,131-digit Integer, and this one costs 0.00s
        against 58s. The "4300 digits" assertion is what pins WHICH failure
        this is -- the printer, not the character ceiling, which has its own
        class above and its own wording."""
        out = symbolic_math.run({"operation": "simplify",
                                 "expression": "factorial(3000)"})
        assert "error" in out
        assert "4300 digits" in out["error"]
        assert "smaller" in out["error"].lower()

    def test_linear_algebra_reports_its_own_unprintable_result(self):
        out = linear_algebra.run({"operation": "determinant",
                                  "matrix_a": [["factorial(3000)", "0"],
                                               ["0", "1"]]})
        assert "error" in out
        assert "determinant" in out["error"]
        assert "4300 digits" in out["error"]
        assert "smaller" in out["error"].lower()

    def test_geometry_reports_its_own_unprintable_result(self):
        out = geometry.run({"operation": "distance",
                            "points": [["factorial(3000)", "0"],
                                       ["0", "0"]]})
        assert "error" in out
        assert "distance" in out["error"]
        assert "4300 digits" in out["error"]
        assert "smaller" in out["error"].lower()

    def test_the_message_names_no_filesystem_path(self):
        """The second half of why this mattered. The generic wrapper
        described CPython to a model that can only change its inputs, and
        it did it while quoting a traceback with a username in it -- which
        the TUI's `TranscriptLogHandler` also draws on screen at WARNING."""
        out = linear_algebra.run({"operation": "determinant",
                                  "matrix_a": [["factorial(3000)", "0"],
                                               ["0", "1"]]})
        lowered = out["error"].lower()
        for leak in ("traceback", "site-packages", "appdata", "\\users\\",
                     ".py\"", "sympy/printing"):
            assert leak not in lowered, (
                "the refusal quotes %r, which is what this finding was "
                "about: %s" % (leak, out["error"]))

    def test_an_ordinary_result_from_each_is_unaffected(self):
        """The discriminating half, one per tool."""
        assert linear_algebra.run(
            {"operation": "determinant",
             "matrix_a": [["1", "2"], ["3", "4"]]})["result"] == "-2"
        assert geometry.run(
            {"operation": "distance",
             "points": [["0", "0"], ["3", "4"]]})["result"] == "5"
        assert symbolic_math.run(
            {"operation": "differentiate",
             "expression": "x**3"})["result"] == "3*x**2"

    def test_discrete_math_still_answers_the_same_way_it_did(self):
        """H6's original site is the control for the whole class: the fix
        moved the guard to what the other three share WITHOUT changing the
        one tool that already had it."""
        out = discrete_math.run({"operation": "factorial", "n": 100000})
        assert "error" in out and "smaller" in out["error"].lower()


class TestATruthTableIsRefusedBeforeItIsEnumerated:
    """S06-02's other half. `truth_table` materialises 2**n rows into a
    list where n is the number of free symbols in a model-supplied string:

        columns   rows    chars returned   secs
           8       256         33,575      0.01   <- the last one allowed
           9       512         73,511      0.03
          14    16,384      3,440,679      1.36   <- reached the model
          15    32,768      7,323,687      3.03

    Refused on the INPUT, which is H5's trade and not a reversal of H4:
    H4 declines a ceiling on a math PARAMETER because cost is joint, and a
    free-symbol count is neither a parameter nor about cost.
    """

    def test_a_table_above_the_ceiling_is_refused(self):
        expr = " & ".join("v%d" % i for i in range(9))
        out = logic.run({"operation": "truth_table", "expression": expr,
                         "variables": ["v%d" % i for i in range(9)]})
        assert "error" in out

    def test_the_refusal_names_the_count_and_the_ceiling(self):
        """The model can only act on a number it is told. The message names
        what it asked for, what the limit is, and the two operations that
        answer the same question without enumerating."""
        expr = " & ".join("v%d" % i for i in range(12))
        out = logic.run({"operation": "truth_table", "expression": expr,
                         "variables": ["v%d" % i for i in range(12)]})
        assert "12 columns" in out["error"]
        assert "8 columns" in out["error"]
        assert "satisfiable" in out["error"]

    def test_the_rows_are_never_built(self):
        """H5's property, stated so it cannot pass by accident.

        A check placed AFTER the enumeration would satisfy every other test
        in this class while still paying 1.36s and 3.4 MB to produce rows
        it then threw away. The double raises if it is reached, so the only
        way this passes is if the refusal happens first.
        """
        calls = []

        def _must_not_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError(
                "truth_table was enumerated before the ceiling was checked")

        expr = " & ".join("v%d" % i for i in range(14))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(logic, "truth_table", _must_not_run)
            out = logic.run({"operation": "truth_table", "expression": expr,
                             "variables": ["v%d" % i for i in range(14)]})
        assert "error" in out
        assert calls == []

    def test_a_table_at_the_ceiling_is_still_answered(self):
        """The discriminating half: 8 columns is 256 rows and 33,575
        characters, which is under `max_read_chars` and is a table a caller
        can legitimately want."""
        expr = " & ".join("v%d" % i for i in range(8))
        out = logic.run({"operation": "truth_table", "expression": expr,
                         "variables": ["v%d" % i for i in range(8)]})
        assert "error" not in out
        assert len(out["result"]) == 256
        assert sum(1 for row in out["result"] if row["output"]) == 1

    def test_declared_names_count_toward_the_ceiling(self):
        """The ceiling is on the COLUMNS, not on the expression's own free
        symbols -- because `variables` now decides the column set, so ten
        declared names over a two-symbol expression is 1,024 rows."""
        out = logic.run({"operation": "truth_table", "expression": "a & b",
                         "variables": ["a", "b"] + ["z%d" % i
                                                    for i in range(9)]})
        assert "error" in out and "11 columns" in out["error"]

    def test_satisfiable_and_tautology_are_not_bounded(self):
        """What the refusal tells the model to do instead has to work. These
        two answer whether a row exists without building any."""
        expr = " & ".join("v%d" % i for i in range(14))
        names = ["v%d" % i for i in range(14)]
        assert logic.run({"operation": "satisfiable", "expression": expr,
                          "variables": names})["result"] is True
        assert logic.run({"operation": "tautology", "expression": expr,
                          "variables": names})["result"] is False


# ===========================================================================
# ---- S06-04: permutations costs what the operation costs -----------------
# ===========================================================================

class TestPermutationsCostsWhatTheOperationCosts:
    """nPr was `factorial(n) / factorial(n - r)`, so its cost was in `n`
    while the operation's cost is in `r` -- and `combinations`, four lines
    below in the same chain, was already O(r) through `binomial`:

        permutations n=1000000 r=1    15.27s of a 20s budget
        permutations n=3000000 r=1    killed at the wall clock, 48.07s
        combinations n=3000000 r=1     0.0002s  ->  3000000

    At r=1 the two return the same number. The model was told "the inputs
    given are too large for this operation" about one multiplication.
    """

    def test_a_large_n_with_r_of_one_is_immediate(self):
        import time
        started = time.time()
        out = discrete_math.run({"operation": "permutations",
                                 "n": 1000000, "r": 1})
        elapsed = time.time() - started
        assert out == {"result": "1000000", "operation": "permutations"}
        assert elapsed < 1.0, (
            "nP1 of a million took %.2fs; the old factorial form took "
            "15.27s and the operation is one multiplication" % elapsed)

    def test_it_agrees_with_combinations_at_r_of_one(self):
        """The control that was in every row of the measurement: at r=1 the
        two operations are the same question."""
        for n in (10, 100000, 1000000):
            perms = discrete_math.run({"operation": "permutations",
                                       "n": n, "r": 1})
            combs = discrete_math.run({"operation": "combinations",
                                       "n": n, "r": 1})
            assert perms["result"] == combs["result"] == str(n)

    @pytest.mark.parametrize("n,r,expected", [
        (10, 3, "720"),          # the textbook case
        (5, 5, "120"),           # r == n is n!
        (5, 0, "1"),             # r == 0 is the empty arrangement
        (52, 5, "311875200"),    # five cards from a deck, in order
    ])
    def test_the_ordinary_answers_are_unchanged(self, n, r, expected):
        assert discrete_math.run({"operation": "permutations",
                                  "n": n, "r": r})["result"] == expected

    @pytest.mark.parametrize("n,r", [(5, 7), (10, -1), (0, 0)])
    def test_the_edges_agree_with_the_form_it_replaced(self, n, r):
        """`ff` is not merely faster, it has to be the same function. These
        are the three inputs where the two definitions could diverge:
        r > n divided by `factorial(-2)`, which is `zoo`, so the old form
        answered 0; and a negative r gave a Rational."""
        old = sympy.factorial(n) / sympy.factorial(n - r)
        out = discrete_math.run({"operation": "permutations",
                                 "n": n, "r": r})
        assert out["result"] == str(old)


# ===========================================================================
# ---- S06-05: a bytes literal is a string-valued construct ----------------
# ===========================================================================

class TestABytesLiteralIsRefusedLikeAStringLiteral:
    """The refusal tested `isinstance(node.value, str)`, so a `bytes`
    constant walked past it into a position no legitimate expression can
    occupy -- `factor(b"1+1")` returned `b'1+1'`, `roots(b"x**2-1")`
    returned `{}` ("no roots" for x^2-1).

    Not a code-execution route on the pinned versions, and that was
    measured rather than assumed: the suite's own witness-import probe,
    extended from `str` payloads to `bytes` payloads across all 213
    allowlisted callables under WSL, found 117 of 213 evaluate a `str` and
    0 of 213 evaluate a `bytes`. The 117 reproduces AGENTS.md's documented
    figure exactly, which is what makes the 0 a measurement.

    Fixed anyway because the design is an allowlist precisely so that it
    does not depend on that holding for the next SymPy -- and `bytes` is
    the one non-`str` type a future evaluator would land on, since `eval()`
    and `compile()` accept it where they reject everything else.
    """

    @pytest.mark.parametrize("payload", [
        'factor(b"1+1")',
        'b"abc"',
        'roots(b"x**2-1")',
        'cancel(b"x/x")',
        'Symbol(b"x")',
    ])
    def test_a_bytes_literal_is_refused_by_the_string_rule(self, payload):
        """Asserting the guard's OWN message, per
        `test_safe_parse_refuses_the_injection_payload_class`'s house rule:
        a payload that fails for an unrelated reason must not read as a
        refusal.

        `Symbol(b"x")` is in the list on purpose. `Symbol` is one of the two
        licensed string positions, and the licensing pass licenses `str`
        only -- `stringify_expr` never emits a bytes argument, so there is
        no expression that needs one.
        """
        with pytest.raises(MathParseError) as excinfo:
            safe_parse(payload)
        assert "string literals are not allowed" in str(excinfo.value)

    @pytest.mark.parametrize("value", [
        b"1+1",
        bytearray(b"x"),
        [b"x"],
        {"k": b"x"},
        (1, [b"x"]),
    ])
    def test_the_output_backstop_refuses_raw_bytes(self, value):
        """The second half: `_reject_escaped_value` let bytes through at
        every depth, because bytes is not a module, a type, a function, or
        one of the four containers it recurses into. `b'1+1'` then reached
        `serialize`, which rendered it as the string `"b'1+1'"`."""
        with pytest.raises(MathParseError) as excinfo:
            _reject_escaped_value(value)
        assert "bytes" in str(excinfo.value)

    def test_the_str_spelling_was_already_refused(self):
        """The control the finding was measured against: the same payloads
        in `str` form were always refused, which is what made the `bytes`
        acceptance a near-miss on a type check rather than a missing rule.
        """
        for payload in ('factor("1+1")', '"abc"', 'cancel("1+1")'):
            with pytest.raises(MathParseError) as excinfo:
                safe_parse(payload)
            assert "string literals are not allowed" in str(excinfo.value)

    def test_the_two_licensed_string_positions_still_work(self):
        """The discriminating half. Widening a type check is one character
        away from refusing the two positions the parser itself emits, and
        every expression with a free variable or a decimal goes through
        them."""
        assert safe_parse("x + 1.5").free_symbols == {Symbol("x")}
        assert safe_parse("1.5") == sympy.Float("1.5")


# ===========================================================================
# ---- S06-06 / S06-07: logic's parameter and logic's domain ---------------
# ===========================================================================

class TestLogicHonoursTheVariablesItDocuments:
    """`variables` was described as *"Explicit variable names/order --
    mainly useful for truth_table"*, and `truth_table` was the one
    operation that never received it: its column list was
    `sorted(expr.free_symbols, key=str)` regardless.

    So both halves of the description were inert while the field was
    silently load-bearing for something it did not mention -- passing
    `symbols=` into `safe_parse`, which is the only thing stopping a
    multi-character variable from being split into single letters.
    """

    def test_the_column_order_follows_the_declared_order(self):
        out = logic.run({"operation": "truth_table", "expression": "a & b",
                         "variables": ["b", "a"]})
        assert list(out["result"][0]["inputs"]) == ["b", "a"]

    def test_a_declared_name_absent_from_the_expression_is_a_column(self):
        """What a caller asking for an explicit order wants: a constant
        column, not a silently dropped name."""
        out = logic.run({"operation": "truth_table", "expression": "a & b",
                         "variables": ["b", "a", "c"]})
        assert list(out["result"][0]["inputs"]) == ["b", "a", "c"]
        assert len(out["result"]) == 8

    def test_an_incomplete_list_still_covers_every_free_symbol(self):
        """The trap that makes this a function rather than one expression,
        and it was measured: handed a list that does not cover every free
        symbol, SymPy's `truth_table` leaves the rest UNSUBSTITUTED and
        returns the symbol itself as the output --
        `truth_table(a & b, [a])` gives `([1], b)`, and `bool(Symbol('b'))`
        is then drawn as `true`.

        So "use the declared names" alone would have replaced an ignored
        parameter with a WRONG table: two rows, and the second one true
        whatever b is. The assertion is the AND truth table, which only one
        column set can produce.
        """
        out = logic.run({"operation": "truth_table", "expression": "a & b",
                         "variables": ["a"]})
        assert len(out["result"]) == 4
        assert [row["output"] for row in out["result"]] == \
            [False, False, False, True]
        for row in out["result"]:
            assert set(row["inputs"]) == {"a", "b"}
            assert all(isinstance(v, bool) for v in row["inputs"].values())

    def test_the_undeclared_split_is_now_said_out_loud(self):
        """The case the old description did not name and the only one where
        omitting the field changes the ANSWER: `rain` parses as `a*i*n*r`,
        so the table was 16 rows over a, i, n, r returned as a success.

        It is refused by the domain check now -- a Mul has no truth value
        -- and the refusal names the split, because "parsed as Mul" is not
        something a caller who wrote a sensible variable name can act on.
        """
        out = logic.run({"operation": "truth_table", "expression": "rain"})
        assert "error" in out
        assert "rain" in out["error"]
        assert "variables=['rain']" in out["error"]

        declared = logic.run({"operation": "truth_table",
                              "expression": "rain",
                              "variables": ["rain"]})
        assert len(declared["result"]) == 2
        assert "warnings" not in declared

    def test_a_split_name_that_survives_the_domain_check_is_warned_about(self):
        """The fourth parsing tool, which batch 131 did not reach.

        `logic` could already reach `symbols=` through `variables`, so it
        was left out of batch 131's `rewritten` plumbing -- but the
        FUNCTIONAL spellings parse to something Boolean, pass the domain
        check, and answer about a product of single letters in silence.
        Only the infix spellings fail loudly, and they fail naming SymPy's
        operand types.
        """
        out = logic.run({"operation": "satisfiable",
                         "expression": "Xor(rain, b)"})
        assert out["result"] is True
        assert out["warnings"] and "rain" in out["warnings"][0]
        assert "variables=['rain']" in out["warnings"][0]

        fixed = logic.run({"operation": "satisfiable",
                           "expression": "Xor(rain, b)",
                           "variables": ["rain"]})
        assert fixed["result"] is True
        assert "warnings" not in fixed

    def test_the_remedy_names_the_field_each_tool_actually_has(self):
        """One sentence, shared -- but the field is `symbols` in three tools
        and `variables` in this one, so a hardcoded `symbols=` would be one
        copy that is wrong for a quarter of its callers."""
        in_logic = logic.run({"operation": "satisfiable",
                              "expression": "Xor(rain, b)"})["warnings"][0]
        in_sibling = linear_algebra.run(
            {"operation": "determinant",
             "matrix_a": [["mass", "0"], ["0", "mass"]]})["warnings"][0]
        assert "variables=['rain']" in in_logic
        assert "symbols=" not in in_logic
        assert "symbols=['mass']" in in_sibling


class TestLogicRefusesWhatHasNoTruthValue:
    """`safe_parse` admits the whole 230-name allowlist, so `logic`'s
    `expression` need not be Boolean, and no operation checked. SymPy's
    `truth_table` substitutes 0/1 and coerces the arithmetic value through
    `bool()`:

        truth_table "x - 1"  ->  a confident NOT table
        truth_table "x + y"  ->  the truth table of OR
        satisfiable "x + y"  ->  true
        tautology   "x + y"  ->  false

    The second one has a plausible caller: in GF(2) and in coding theory
    `x + y` IS XOR, and the tool answers OR. `bool()` of an arithmetic
    value is not a ground-truth check for a logical claim -- it is a
    coincidence that agrees with OR for `+` and with AND for `*` and
    diverges elsewhere, which is the opposite of what this module's
    docstring promises.
    """

    @pytest.mark.parametrize("expression", [
        "x - 1", "x + y", "x**2", "pi", "2", "Matrix([[1]])", "sin(x)",
    ])
    @pytest.mark.parametrize("operation", [
        "simplify", "satisfiable", "tautology", "truth_table",
    ])
    def test_an_arithmetic_expression_is_refused_by_every_operation(
            self, expression, operation):
        out = logic.run({"operation": operation, "expression": expression})
        assert "error" in out
        assert "no truth value" in out["error"]

    def test_the_refusal_names_the_type_and_the_operators(self):
        """What the caller can act on: what it was read as, and what this
        tool does understand."""
        out = logic.run({"operation": "truth_table", "expression": "x + y"})
        assert "Add" in out["error"]
        for operator in ("&", "|", "~", ">>", "Xor", "Eq(x, 5)"):
            assert operator in out["error"]

    @pytest.mark.parametrize("expression", [
        "a & b", "a | ~a", "a >> b", "~a", "Xor(a, b)", "Equivalent(a, b)",
        "Nand(a, b)", "a & (b | ~c)", "Eq(x, 5)", "x > 0", "Ne(x, 5)",
        "a", "true", "false", "1 = 1",
    ])
    def test_the_propositional_battery_is_untouched(self, expression):
        """The discriminating half, and the reason the check is ONE
        `isinstance` rather than a union of four types: `Symbol`,
        `BooleanTrue`, `BooleanFalse` and every `Relational` are already
        `boolalg.Boolean` subclasses. One name is also closed over whatever
        Boolean type a future SymPy adds, which an enumeration is not.
        """
        out = logic.run({"operation": "satisfiable",
                         "expression": expression})
        assert "error" not in out, out.get("error")

    def test_the_second_expression_is_checked_too(self):
        """`equivalent` parses two. Checking one of them is the shape this
        whole surface keeps producing."""
        out = logic.run({"operation": "equivalent", "expression": "a & b",
                         "expression_b": "x + y"})
        assert "error" in out
        assert "expression_b" in out["error"]

    def test_a_relational_still_produces_a_real_table(self):
        """`x > 0` is a Relational and therefore allowed, and it has to
        remain MEANINGFUL: substituting 0 and 1 for x gives False then
        True, which is the correct table for that proposition."""
        out = logic.run({"operation": "truth_table", "expression": "x > 0"})
        assert [row["output"] for row in out["result"]] == [False, True]


# ===========================================================================
# ---- S06-08 + the rider: a tool's own params are its own error -----------
# ===========================================================================

class TestTheVariableFieldMustNameOneVariable:
    """`symbolic_math.variable` was `str` with no validation, and
    `sympy.Symbol` accepts any string -- so a value that is not an
    identifier became a symbol that cannot occur in the parsed expression,
    and every operation taking a variable had a well-defined wrong answer:

        solve "x**2 - 4 = 0"  variable="x, y"  ->  {"result": []}
        differentiate "x**3"  variable="x, y"  ->  {"result": "0"}

    `[]` is indistinguishable in the result from "this equation has no
    solutions" and `"0"` from a correct zero derivative. The trigger is the
    schema's own invitation: the sibling field `equations` IS a list, and
    the solve-a-system path ignores `variable` entirely.
    """

    @pytest.mark.parametrize("variable", ["x, y", "2x", "", "x y", "x.y"])
    def test_a_non_identifier_is_refused(self, variable):
        out = symbolic_math.run({"operation": "solve",
                                 "expression": "x**2 - 4 = 0",
                                 "variable": variable})
        assert "error" in out
        assert "variable" in out["error"]

    @pytest.mark.parametrize("variable,expected", [
        ("x", ["-2", "2"]),
        ("t_1", []),
    ])
    def test_a_valid_identifier_is_accepted(self, variable, expected):
        """`t_1` is in here as a control on the PATTERN rather than on the
        answer: solving for a symbol that does not occur in the equation is
        correctly `[]`, and the point is that it is not a validation
        error."""
        out = symbolic_math.run({"operation": "solve",
                                 "expression": "x**2 - 4 = 0",
                                 "variable": variable})
        assert out["result"] == expected

    def test_the_default_is_still_valid(self):
        """A pattern that refuses its own default is a tool nobody can
        call."""
        assert symbolic_math.run(
            {"operation": "differentiate",
             "expression": "x**3"})["result"] == "3*x**2"


class TestEveryMathToolRefusesItsOwnBadParams:
    """THE FINDING UNDER S06-08, and the report's suggested shape depended
    on it being false.

    S06-08's remedy is a `pattern` on `variable`, "which turns the case into
    a schema error the model can read". Measured: it does not. Every math
    tool built `Model(**params)` on the line ABOVE its `try`, so a field
    constraint that fired left `run()` as a bare `pydantic.ValidationError`
    -- which means adding a pattern would have traded a wrong answer for
    the traceback path S06-03 is about.

    Measured through the real transport, on the three constraints that
    ALREADY existed (`precision` le=50, `order` ge=1, and the `operation`
    `Literal` itself):

        {"error": "symbolic_math did not return a usable result (exit 1).
         ... line 263, in __init__ ... pydantic_core._pydantic_core.
         ValidationError: 1 validation error for SymbolicMathParams ..."}

    plus the same traceback at WARNING, which the TUI draws on screen.
    `operation="nope"` is the most ordinary mistake a caller can make.
    """

    @pytest.mark.parametrize("module", [
        symbolic_math, linear_algebra, geometry, discrete_math,
        probability_stats, logic,
    ])
    def test_an_unknown_operation_is_an_error_dict(self, module):
        """The relation, not an assertion about any one tool: a tool added
        later with the same `Model(**params)` shape fails here."""
        out = module.run({"operation": "no_such_operation"})
        assert isinstance(out, dict), (
            "%s raised instead of returning" % module.TOOL_SCHEMA["name"])
        assert "error" in out

    @pytest.mark.parametrize("module", [
        symbolic_math, linear_algebra, geometry, discrete_math,
        probability_stats, logic,
    ])
    def test_the_message_is_the_models_vocabulary_not_pydantics(self, module):
        """The discriminating half, and it is what separates this fix from
        `return {"error": str(exc)}`: a `ValidationError`'s own text ends in
        "For further information visit https://errors.pydantic.dev/...",
        names the model class, and on the transport path carries a file and
        a line number."""
        out = module.run({"operation": "no_such_operation"})
        lowered = out["error"].lower()
        for leak in ("pydantic", "traceback", "__init__", "validate_python",
                     "https://"):
            assert leak not in lowered, (
                "%s leaks %r: %s"
                % (module.TOOL_SCHEMA["name"], leak, out["error"]))

    def test_the_refusal_names_the_field_and_what_was_allowed(self):
        """`err["msg"]` is pydantic's own wording, which for a `Literal`
        lists the operations verbatim -- exactly what the caller needs, and
        the one thing the generic transport error could not say."""
        out = symbolic_math.run({"operation": "nope", "expression": "x"})
        assert "'operation'" in out["error"]
        assert "simplify" in out["error"]
        assert "differentiate" in out["error"]

    @pytest.mark.parametrize("params", [
        {"operation": "evaluate", "expression": "pi", "precision": 100},
        {"operation": "differentiate", "expression": "x**2", "order": 0},
        {"operation": "solve", "expression": "x", "variable": "x, y"},
    ])
    def test_the_constraints_that_were_already_there_are_contained_too(
            self, params):
        """`precision` and `order` were validated before this batch and
        their failures took the bug branch. The fix is not about the new
        pattern; the pattern is one more thing the containment covers."""
        out = symbolic_math.run(params)
        assert "error" in out
        assert "parameters were not accepted" in out["error"].lower()

    def test_every_valid_call_is_untouched(self):
        """The control. Containment that also catches the good calls is a
        broken tool."""
        assert symbolic_math.run(
            {"operation": "evaluate", "expression": "pi",
             "precision": 10})["result"].startswith("3.14159")
        assert logic.run(
            {"operation": "tautology",
             "expression": "a | ~a"})["result"] is True
        assert discrete_math.run(
            {"operation": "gcd", "a": 12, "b": 18})["result"] == "6"


# ===========================================================================
# ---- S06-09: the backstop's depth cap fails closed -----------------------
# ===========================================================================

class TestTheBackstopFailsClosedPastItsDepthCap:
    """`_reject_escaped_value`'s recursion guard was `if _depth > 6:
    return`, so the one check designed to catch an escape nobody
    anticipated had an anticipated DEPTH:

        _reject_escaped_value([[[os]]])           -> refused   (depth 3)
        _reject_escaped_value([[[[[[[[os]]]]]]]]) -> ACCEPTED  (depth 8)

    Filed `suspected` rather than `demonstrated` -- no allowlisted callable
    returns a module at any depth, let alone past 7 -- and fixed anyway
    because the comment justifying the `return` ("containers are shallow
    here") was an assertion about today's return shapes inside the one
    function whose stated job is not to rely on them.
    """

    def test_a_module_past_the_cap_is_refused(self):
        deep = sys
        for _ in range(8):
            deep = [deep]
        with pytest.raises(MathParseError):
            _reject_escaped_value(deep)

    def test_the_refusal_is_about_the_depth_and_not_the_contents(self):
        """The discriminating test, and the one that separates this fix from
        deleting the cap altogether.

        With the cap removed the walk would simply recurse further and
        still find a module at depth 8 -- so a test that only nests `os`
        deeper cannot tell the two apart. An INNOCENT value past the cap
        can: `[[[[[[[[1]]]]]]]]` is refused because of where it is, and
        `[[[1]]]` is accepted because the walk reaches it.
        """
        innocent = 1
        for _ in range(8):
            innocent = [innocent]
        with pytest.raises(MathParseError) as excinfo:
            _reject_escaped_value(innocent)
        assert "deep" in str(excinfo.value)

        shallow = 1
        for _ in range(3):
            shallow = [shallow]
        _reject_escaped_value(shallow)

    def test_the_cap_is_not_set_so_tight_it_refuses_a_real_result(self):
        """`test_the_backstop_allows_every_legitimate_return_shape` above is
        the real guard here; this names the deepest shape in it so the
        number has a reason. `eigenvects()` is a list of tuples of lists of
        matrices -- depth 4."""
        out = linear_algebra.run({"operation": "eigenvectors",
                                  "matrix_a": [["1", "2"], ["3", "4"]]})
        assert "error" not in out
