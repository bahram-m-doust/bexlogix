from pathlib import Path

import pandas as pd

from client.components.route_map import _GRADE_STYLES, _build_marker_rows


def test_route_markers_use_each_store_grade_colour():
    grades = ["VIP", "A+", "A", "B", "C"]
    stores = pd.DataFrame(
        [
            {
                "lat": 35.7,
                "lon": 51.4,
                "store_code": f"STR-{index:03d}",
                "store_name": f"Store {grade}",
                "store_grade": grade,
                "route_order": index,
            }
            for index, grade in enumerate(grades, start=1)
        ]
    )

    markers = _build_marker_rows(stores)

    assert [marker["store_grade"] for marker in markers] == grades
    assert [marker["marker_fill"] for marker in markers] == [
        _GRADE_STYLES[grade]["fill"] for grade in grades
    ]
    assert [marker["marker_stroke"] for marker in markers] == [
        _GRADE_STYLES[grade]["stroke"] for grade in grades
    ]


def test_all_leaflet_raster_renderers_overzoom_native_zoom_14_tiles():
    source = (
        Path(__file__).resolve().parents[1]
        / "client"
        / "components"
        / "route_map.py"
    ).read_text(encoding="utf-8")

    assert source.count("maxNativeZoom: 14") == 2
    assert source.count("maxZoom: 19") == 2
