"""
tools/builtin/probability_stats.py

Descriptive statistics on raw data (using the stdlib `statistics` module
-- deterministic, no need for sympy here) and probability distributions
(using sympy.stats, kept exact/symbolic where possible).

Named probability_stats rather than statistics.py specifically to avoid
any reader confusion with the stdlib module of the same name, even though
Python 3's absolute-import default would resolve `import statistics`
correctly regardless.
"""

from __future__ import annotations

import statistics as pystats
from typing import Literal, Optional

import sympy
import sympy.stats as sstats
from pydantic import BaseModel, Field

_OPERATIONS = Literal["describe", "distribution_stats", "distribution_pdf", "distribution_cdf"]
_DISTRIBUTIONS = Literal["normal", "binomial", "poisson", "uniform", "exponential"]


class ProbabilityStatsParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    data: Optional[list[float]] = Field(None, description="Dataset, for the 'describe' operation")
    distribution: Optional[_DISTRIBUTIONS] = Field(
        None, description="Distribution family, for distribution_* operations"
    )
    distribution_params: Optional[dict[str, float]] = Field(
        None,
        description=(
            "Parameters for the chosen distribution: normal={mu, sigma}, "
            "binomial={n, p}, poisson={lambda}, uniform={a, b}, "
            "exponential={rate}"
        ),
    )
    x: Optional[float] = Field(None, description="Point to evaluate pdf/cdf at")
    evaluate: bool = Field(False, description="If true, return numeric approximation (evalf) instead of exact symbolic form (e.g. 0.84... instead of erf(...)). Default false keeps exact symbolic.")
    precision: int = Field(15, ge=1, le=50, description="Significant digits when evaluate is true")


TOOL_SCHEMA = {
    "name": "probability_stats",
    "description": (
        "Compute descriptive statistics on a dataset (mean, median, "
        "variance, stdev, min, max), or work with probability "
        "distributions (normal, binomial, poisson, uniform, exponential): "
        "expected value, variance, pdf at a point, or cdf at a point. "
        "Results are exact symbolic expressions (e.g. erf(...), sqrt(pi)) unless 'evaluate' is true, "
        "which returns a numeric approximation with 'precision' digits. When distribution parameters "
        "are omitted, defaults are used (normal mu=0 sigma=1, uniform a=0 b=1, exponential rate=1) and a "
        "'warnings' field explains what was defaulted."
    ),
    "input_schema": ProbabilityStatsParams.model_json_schema(),
}


def _build_distribution(name: str, dp: dict):
    if name == "normal":
        return sstats.Normal("X", dp.get("mu", 0), dp.get("sigma", 1))
    if name == "binomial":
        return sstats.Binomial("X", int(dp["n"]), dp["p"])
    if name == "poisson":
        return sstats.Poisson("X", dp["lambda"])
    if name == "uniform":
        return sstats.Uniform("X", dp.get("a", 0), dp.get("b", 1))
    if name == "exponential":
        return sstats.Exponential("X", dp.get("rate", 1))
    raise ValueError(f"Unsupported distribution: {name}")


def _build_distribution_with_warnings(name: str, dp_orig: Optional[dict]):
    """Build distribution and collect warnings for defaulted params.

    Returns (dist, warnings). Warnings is a list of strings for each param
    that fell back to a default, per decision 2 (keep forgiving but explain).
    Binomial/poisson have no defaults — missing required keys become a
    friendly error at the caller, not a warning.
    """
    dp = dp_orig or {}
    warnings: list[str] = []
    if name == "normal":
        if "mu" not in dp:
            warnings.append("mu not supplied - defaulted to 0")
        if "sigma" not in dp:
            warnings.append("sigma not supplied - defaulted to 1")
        return sstats.Normal("X", dp.get("mu", 0), dp.get("sigma", 1)), warnings
    if name == "uniform":
        if "a" not in dp:
            warnings.append("a not supplied - defaulted to 0")
        if "b" not in dp:
            warnings.append("b not supplied - defaulted to 1")
        return sstats.Uniform("X", dp.get("a", 0), dp.get("b", 1)), warnings
    if name == "exponential":
        if "rate" not in dp:
            warnings.append("rate not supplied - defaulted to 1")
        return sstats.Exponential("X", dp.get("rate", 1)), warnings
    if name == "binomial":
        # No defaults — caller validates presence
        return sstats.Binomial("X", int(dp["n"]), dp["p"]), warnings
    if name == "poisson":
        return sstats.Poisson("X", dp["lambda"]), warnings
    raise ValueError(f"Unsupported distribution: {name}")


def _maybe_evalf(value, do_eval: bool, precision: int):
    if not do_eval or value is None:
        return value
    try:
        if hasattr(value, "evalf"):
            return value.evalf(precision)
    except Exception:
        pass
    return value


def _cdf_at(dist, x):
    """Evaluate CDF at x, handling discrete binomial's dict return."""
    try:
        return sstats.cdf(dist)(x)
    except TypeError as e:
        if "'dict' object is not callable" in str(e):
            # Binomial (and some discretes) return a dict table via two-arg form
            table = sstats.cdf(dist, x)
            if isinstance(table, dict):
                import math

                try:
                    # Discrete CDF at x is sum up to floor(x)
                    key = int(math.floor(float(x)))
                except Exception:
                    return table
                if key in table:
                    return table[key]
                # find greatest key <= x
                sorted_keys = sorted(table.keys())
                candidate = None
                for k in sorted_keys:
                    if k <= key:
                        candidate = table[k]
                    else:
                        break
                if candidate is not None:
                    return candidate
                if key < sorted_keys[0]:
                    return sympy.Integer(0)
                return sympy.Integer(1)
            return table
        raise


def run(params: dict) -> dict:
    parsed = ProbabilityStatsParams(**params)

    try:
        if parsed.operation == "describe":
            data = parsed.data
            if not data:
                return {"error": "data is required for 'describe'"}
            result = {
                "mean": pystats.mean(data),
                "median": pystats.median(data),
                "stdev": pystats.stdev(data) if len(data) > 1 else 0.0,
                "variance": pystats.variance(data) if len(data) > 1 else 0.0,
                "min": min(data),
                "max": max(data),
                "n": len(data),
            }
            return {"result": result, "operation": parsed.operation}

        if not parsed.distribution:
            return {"error": "distribution is required for this operation"}

        dp = parsed.distribution_params or {}
        # Friendly validation for distributions with no defaults
        if parsed.distribution == "binomial":
            if "n" not in dp or "p" not in dp:
                return {"error": "binomial requires 'n' (number of trials) and 'p' (success probability) in distribution_params."}
        if parsed.distribution == "poisson":
            if "lambda" not in dp:
                return {"error": "poisson requires 'lambda' in distribution_params."}

        dist, warnings = _build_distribution_with_warnings(parsed.distribution, parsed.distribution_params)

        if parsed.operation == "distribution_stats":
            mean_v = _maybe_evalf(sstats.E(dist), parsed.evaluate, parsed.precision)
            var_v = _maybe_evalf(sstats.variance(dist), parsed.evaluate, parsed.precision)
            # std is sqrt(var) — eval the sqrt when numeric
            raw_var = sstats.variance(dist)
            std_v = _maybe_evalf(sympy.sqrt(raw_var), parsed.evaluate, parsed.precision)
            result = {
                "mean": str(mean_v),
                "variance": str(var_v),
                "std": str(std_v),
            }
        elif parsed.operation == "distribution_pdf":
            if parsed.x is None:
                return {"error": "x is required for distribution_pdf"}
            x_arg = parsed.x
            # Discrete distributions expect int; float 5.0 should be treated as 5
            if parsed.distribution in ("binomial", "poisson") and isinstance(x_arg, float) and x_arg.is_integer():
                x_arg = int(x_arg)
            raw = sstats.density(dist)(x_arg)
            val = _maybe_evalf(raw, parsed.evaluate, parsed.precision)
            result = str(val)
        elif parsed.operation == "distribution_cdf":
            if parsed.x is None:
                return {"error": "x is required for distribution_cdf"}
            x_arg = parsed.x
            if parsed.distribution in ("binomial", "poisson") and isinstance(x_arg, float) and x_arg.is_integer():
                x_arg = int(x_arg)
            raw = _cdf_at(dist, x_arg)
            val = _maybe_evalf(raw, parsed.evaluate, parsed.precision)
            result = str(val)
        else:
            return {"error": f"Unknown operation: {parsed.operation}"}

    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    out: dict = {"result": result, "operation": parsed.operation}
    if warnings:
        out["warnings"] = warnings
    return out
