"""
tools/builtin/geometry.py

Classical 2D/3D geometry: distances, midpoints, lines, circles, polygons,
and triangles. Matrix/vector/tensor work lives in linear_algebra.py --
this tool is specifically for geometric objects and their properties.
"""

from __future__ import annotations

from typing import Literal, Optional

from sympy.geometry import Point, Line, Circle, Polygon, Triangle
from pydantic import BaseModel, Field

from tools.builtin._math_common import safe_parse, serialize, MathParseError

_OPERATIONS = Literal[
    "distance", "midpoint", "is_collinear", "line_intersection",
    "circle_area", "circle_circumference",
    "polygon_area", "polygon_perimeter", "triangle_properties",
]


class GeometryParams(BaseModel):
    operation: _OPERATIONS = Field(..., description="Which operation to perform")
    points: Optional[list[list[str]]] = Field(
        None, description="List of points, each as [x, y] coordinate strings (numbers or symbolic expressions)"
    )
    radius: Optional[str] = Field(None, description="Radius, for circle operations (required for circle_area/circle_circumference)")
    center: Optional[list[str]] = Field(None, description="Center point [x, y] for circle operations - optional, defaults to [0, 0] (origin) when omitted")
    evaluate: bool = Field(False, description="If true, return numeric approximation (e.g. 12.566... instead of 4*pi). Default false keeps exact symbolic form.")
    precision: int = Field(15, ge=1, le=50, description="Significant digits when evaluate is true")


TOOL_SCHEMA = {
    "name": "geometry",
    "description": (
        "Classical geometry operations: distance and midpoint between "
        "points, collinearity check, line intersection, circle area and "
        "circumference, polygon area and perimeter, and triangle "
        "properties (area, perimeter, right/equilateral/isosceles checks). "
        "Circle operations default the center to the origin when not supplied; results are exact symbolic "
        "expressions (e.g. 4*pi, sqrt(2)) unless 'evaluate' is true, which returns a numeric approximation with 'precision' digits."
    ),
    "input_schema": GeometryParams.model_json_schema(),
}


def _to_point(coords: list[str]) -> Point:
    return Point(*[safe_parse(c) for c in coords])


def run(params: dict) -> dict:
    parsed = GeometryParams(**params)

    try:
        op = parsed.operation
        pts = [_to_point(p) for p in parsed.points] if parsed.points else []

        if op == "distance":
            if len(pts) < 2:
                return {"error": "distance requires 2 points (each [x, y])."}
            result = pts[0].distance(pts[1])
        elif op == "midpoint":
            if len(pts) < 2:
                return {"error": "midpoint requires 2 points (each [x, y])."}
            result = pts[0].midpoint(pts[1])
        elif op == "is_collinear":
            if len(pts) < 2:
                return {"error": "is_collinear requires at least 2 points."}
            result = Point.is_collinear(*pts)
        elif op == "line_intersection":
            if len(pts) < 4:
                return {"error": "line_intersection requires 4 points (two per line: [p0,p1] and [p2,p3])."}
            if pts[0] == pts[1]:
                return {"error": "line_intersection: the first two points must be distinct (first line is degenerate)."}
            if pts[2] == pts[3]:
                return {"error": "line_intersection: the last two points must be distinct (second line is degenerate)."}
            line_a = Line(pts[0], pts[1])
            line_b = Line(pts[2], pts[3])
            result = line_a.intersection(line_b)
        elif op == "circle_area":
            if parsed.radius is None:
                return {"error": "circle_area requires 'radius'."}
            center = _to_point(parsed.center) if parsed.center is not None else Point(0, 0)
            result = Circle(center, safe_parse(parsed.radius)).area
        elif op == "circle_circumference":
            if parsed.radius is None:
                return {"error": "circle_circumference requires 'radius'."}
            center = _to_point(parsed.center) if parsed.center is not None else Point(0, 0)
            result = Circle(center, safe_parse(parsed.radius)).circumference
        elif op == "polygon_area":
            if len(pts) < 3:
                return {"error": f"polygon_area requires at least 3 points (got {len(pts)})."}
            # Pre-check collinear/degenerate that would surface as Segment2D
            if len(pts) == 3 and Point.is_collinear(*pts):
                return {"error": "polygon_area: degenerate polygon (collinear points) - at least 3 non-collinear points required."}
            result = Polygon(*pts).area
        elif op == "polygon_perimeter":
            if len(pts) < 3:
                return {"error": f"polygon_perimeter requires at least 3 points (got {len(pts)})."}
            if len(pts) == 3 and Point.is_collinear(*pts):
                return {"error": "polygon_perimeter: degenerate polygon (collinear points) - at least 3 non-collinear points required."}
            result = Polygon(*pts).perimeter
        elif op == "triangle_properties":
            if len(pts) != 3:
                return {"error": f"triangle_properties requires exactly 3 points (got {len(pts)})."}
            if Point.is_collinear(*pts):
                return {"error": "triangle_properties: degenerate triangle (collinear points) - Segment2D has no triangle properties."}
            tri = Triangle(*pts)
            if parsed.evaluate:
                result = {
                    "area": str(tri.area.evalf(parsed.precision)),
                    "perimeter": str(tri.perimeter.evalf(parsed.precision)),
                    "is_right": tri.is_right(),
                    "is_equilateral": tri.is_equilateral(),
                    "is_isosceles": tri.is_isosceles(),
                }
            else:
                result = {
                    "area": str(tri.area),
                    "perimeter": str(tri.perimeter),
                    "is_right": tri.is_right(),
                    "is_equilateral": tri.is_equilateral(),
                    "is_isosceles": tri.is_isosceles(),
                }
        else:
            return {"error": f"Unknown operation: {op}"}

    except MathParseError as e:
        return {"error": str(e)}
    except (IndexError, TypeError) as e:
        return {"error": f"Missing or malformed points/parameters for '{op}': {e}"}
    except Exception as e:
        return {"error": f"Computation failed: {e}"}

    # Numeric-evaluate path: keep exact by default, evaluate only when asked.
    if parsed.evaluate and not isinstance(result, (bool, dict)):
        try:
            if isinstance(result, list):
                result = [r.evalf(parsed.precision) if hasattr(r, "evalf") else r for r in result]
            elif hasattr(result, "evalf"):
                result = result.evalf(parsed.precision)
        except Exception:
            pass

    return {"result": serialize(result), "operation": op}
