"""
tartarus_svg.py
Minimal SVG transform-attribute parsing, used by tartarus_gui.py to read exact
hitboxes off tartarus_v2.svg's own <rect>/<circle>/<ellipse> elements (Key_1..
Key_20, Circle, Cross, Scroll - see that file). Inkscape normally bakes a
rotation into a path's point coordinates; with Edit > Preferences > Behavior >
Transforms set to "Preserved" instead, rotating/skewing a shape leaves a
literal transform="matrix(...)" attribute on the element instead, which
SvgDocument.local_geometry() resolves (composed with all ancestor transforms)
into the shape's local geometry plus that full matrix - see its docstring for
why callers should apply the matrix as-is rather than reducing it to a
rotation angle.

Only <rect>/<circle>/<ellipse> elements are handled (they carry their own
local geometry to transform). Plain <path> elements (older, baked-rotation
shapes) aren't handled here - tartarus_gui.py falls back to
QSvgRenderer.boundsOnElement() (axis-aligned) for those.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
Matrix = tuple[float, float, float, float, float, float]  # a, b, c, d, e, f
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def compose(m1: Matrix, m2: Matrix) -> Matrix:
    """Composes two SVG-style affine matrices: applying the result to a point
    is equivalent to applying m2 first, then m1 (matches SVG's own semantics
    for `transform="m1 m2"` and for parent-then-child transform chains)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def parse_transform(value: str | None) -> Matrix:
    """Parses an SVG `transform` attribute value into a single affine matrix.
    Supports matrix/translate/scale/rotate (with or without a rotation
    center); skewX/skewY are not needed for this artwork and are ignored."""
    if not value:
        return IDENTITY
    result = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", value):
        nums = [float(x) for x in re.split(r"[,\s]+", args.strip()) if x]
        if name == "matrix" and len(nums) == 6:
            m = tuple(nums)
        elif name == "translate":
            m = (1, 0, 0, 1, nums[0], nums[1] if len(nums) > 1 else 0)
        elif name == "scale":
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            m = (sx, 0, 0, sy, 0, 0)
        elif name == "rotate":
            angle = math.radians(nums[0])
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            m = (cos_a, sin_a, -sin_a, cos_a, 0, 0)
            if len(nums) > 2:
                cx, cy = nums[1], nums[2]
                m = compose(compose((1, 0, 0, 1, cx, cy), m), (1, 0, 0, 1, -cx, -cy))
        else:
            continue
        result = compose(result, m)
    return result


class SvgDocument:
    """Parses an SVG once and answers "what's the exact local geometry and
    transform for element X" queries against its <rect>/<circle>/<ellipse>
    elements - see local_geometry()."""

    def __init__(self, svg_path: Path):
        self._tree = ET.parse(svg_path)
        self._root = self._tree.getroot()
        self._parent = {child: parent for parent in self._root.iter() for child in parent}

    def _find(self, element_id: str) -> ET.Element | None:
        return self._root.find(f".//*[@id='{element_id}']")

    def _ancestor_transform(self, element: ET.Element) -> Matrix:
        """Composes the transform of every ancestor from the document root
        down to (not including) `element` itself."""
        chain = []
        node = self._parent.get(element)
        while node is not None:
            chain.append(node)
            node = self._parent.get(node)
        m = IDENTITY
        for ancestor in reversed(chain):
            m = compose(m, parse_transform(ancestor.get("transform")))
        return m

    def local_geometry(
        self, element_id: str
    ) -> tuple[str, tuple[float, float, float, float], Matrix] | None:
        """Returns (shape_kind, local_rect, matrix) for a <rect>, <circle> or
        <ellipse> element with the given id, or None if it doesn't exist / is
        some other tag.

        local_rect = (x, y, w, h) is in the element's OWN pre-transform
        coordinate space (for circle/ellipse, its bounding box); matrix is the
        full ancestor-chain-plus-own-transform, applied as-is (not decomposed
        into a rotation angle). Using the full matrix - rather than reducing
        it to a rotation angle - means shear or non-uniform scale (which a
        freehand Inkscape rotate/skew can leave behind, e.g. mixed with the
        document's own Y-flip) still renders and hit-tests exactly like the
        source shape instead of silently getting flattened to the nearest
        pure rotation."""
        el = self._find(element_id)
        if el is None:
            return None

        if el.tag == f"{{{SVG_NS}}}rect":
            shape_kind = "rect"
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
        elif el.tag == f"{{{SVG_NS}}}circle":
            shape_kind = "ellipse"
            r = float(el.get("r", 0))
            cx0, cy0 = float(el.get("cx", 0)), float(el.get("cy", 0))
            x, y, w, h = cx0 - r, cy0 - r, 2 * r, 2 * r
        elif el.tag == f"{{{SVG_NS}}}ellipse":
            shape_kind = "ellipse"
            rx, ry = float(el.get("rx", 0)), float(el.get("ry", 0))
            cx0, cy0 = float(el.get("cx", 0)), float(el.get("cy", 0))
            x, y, w, h = cx0 - rx, cy0 - ry, 2 * rx, 2 * ry
        else:
            return None

        matrix = compose(self._ancestor_transform(el), parse_transform(el.get("transform")))
        return shape_kind, (x, y, w, h), matrix
