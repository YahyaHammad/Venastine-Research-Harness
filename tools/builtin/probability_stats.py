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


TOOL_SCHEMA = {
    "name": "probability_stats",
    "description": (
        "Compute descriptive statistics on a dataset (mean, median, "
        "variance, stdev, min, max), or work with probability "
        "distributions (normal, binomial, poisson, uniform, exponential): "
        "expected value, variance, pdf at a point, or cdf at a point."
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

        dist = _build_distribution(parsed.distribution, parsed.distribution_params or {})

        if parsed.operation == "distribution_stats":
            result = {
                "mean": str(sstats.E(dist)),
                "variance": str(sstats.variance(dist)),
                "std": str(sympy.sqrt(sstats.variance(dist))),
            }
        elif parsed.operation == "distribution_pdf":
            if parsed.x is None:
                return {"error": "x is required for distribution_pdf"}
            result = str(sstats.density(dist)(parsed.x))
        elif parsed.operation == "distribution_cdf":
            if parsed.x is None:
                return {"error": "x is required for distribution_cdf"}
            result = str(sstats.cdf(dist)(parsed.x))
        else:
            return {"error": f"Unknown operation: {parsed.operation}"}

    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    return {"result": result, "operation": parsed.operation}
