# Purpose: Route map rendering component for assignment and visitor workflows.
# Workflow Role: Renders an offline-safe interactive map with OSRM/Tile diagnostics.

from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from client.components.tehran_overlay import get_minimal_background_geojson
from server.app import config
from server.app.services import routing_service, runtime_health_service

_GRADE_STYLES: dict[str, dict[str, str]] = {
    "VIP": {"fill": "#7E57C2", "stroke": "#5E35B1", "label": "VIP"},
    "A+": {"fill": "#42A5F5", "stroke": "#1E88E5", "label": "A+"},
    "A": {"fill": "#66BB6A", "stroke": "#43A047", "label": "A"},
    "B": {"fill": "#FFB74D", "stroke": "#FB8C00", "label": "B"},
    "C": {"fill": "#EF5350", "stroke": "#E53935", "label": "C"},
}
_DEFAULT_GRADE_STYLE: dict[str, str] = {
    "fill": "#6B7280",
    "stroke": "#374151",
    "label": "نامشخص",
}

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_LEAFLET_DIR = _ASSETS_DIR / "vendor" / "leaflet"
_LEAFLET_IMAGES = _LEAFLET_DIR / "images"


# Contract: _load_leaflet_js loads local vendored Leaflet JavaScript once.
@lru_cache(maxsize=1)
def _load_leaflet_js() -> str:
    path = _LEAFLET_DIR / "leaflet.js"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# Contract: _load_leaflet_css loads local vendored Leaflet CSS once and inlines image assets.
@lru_cache(maxsize=1)
def _load_leaflet_css() -> str:
    path = _LEAFLET_DIR / "leaflet.css"
    if not path.exists():
        return ""

    css_text = path.read_text(encoding="utf-8")
    for filename in (
        "layers.png",
        "layers-2x.png",
        "marker-icon.png",
        "marker-icon-2x.png",
        "marker-shadow.png",
    ):
        image_path = _LEAFLET_IMAGES / filename
        if not image_path.exists():
            continue
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        css_text = css_text.replace(
            f"url(images/{filename})",
            f"url(data:image/png;base64,{encoded})",
        )
    return css_text


# Contract: _to_py_scalar normalizes numpy/pandas values to plain Python scalars.
def _to_py_scalar(value):
    if value is None:
        return None
    if pd.isna(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(value)


# Contract: _normalize_grade returns canonical uppercase grade text.
def _normalize_grade(value: object) -> str:
    return str(value or "").strip().upper()


# Contract: _grade_style maps grade to marker style with default fallback.
def _grade_style(value: object) -> dict[str, str]:
    return _GRADE_STYLES.get(_normalize_grade(value), _DEFAULT_GRADE_STYLE)


# Contract: _extract_start_point resolves visitor start point from explicit args or dataframe columns.
def _extract_start_point(
    stops_df: pd.DataFrame,
    start_lat: float | None,
    start_lon: float | None,
) -> tuple[float, float] | None:
    if start_lat is not None and start_lon is not None:
        return float(start_lat), float(start_lon)

    if {"start_lat", "start_lon"}.issubset(stops_df.columns):
        candidates = (
            stops_df[["start_lat", "start_lon"]]
            .dropna(subset=["start_lat", "start_lon"])
            .head(1)
        )
        if not candidates.empty:
            row = candidates.iloc[0]
            return float(row["start_lat"]), float(row["start_lon"])
    return None


# Contract: _build_straight_path creates linear fallback geometry in lon/lat order.
def _build_straight_path(
    start_point: tuple[float, float] | None,
    ordered_points: pd.DataFrame,
) -> list[list[float]]:
    path_points: list[list[float]] = []
    if start_point is not None:
        path_points.append([float(start_point[1]), float(start_point[0])])  # lon, lat
    for _, row in ordered_points.iterrows():
        path_points.append([float(row["lon"]), float(row["lat"])])  # lon, lat
    return path_points


# Contract: _build_route_geometry_path resolves geometry from OSRM when healthy, else linear fallback.
def _build_route_geometry_path(
    start_point: tuple[float, float] | None,
    ordered_points: pd.DataFrame,
    runtime_status: dict[str, object],
) -> tuple[list[list[float]], str]:
    if start_point is None or ordered_points.empty:
        return _build_straight_path(start_point, ordered_points), "straight"

    # FIX: [PERF-02] Fail fast when OSRM is down and skip heavy geometry retries.
    if not bool(runtime_status.get("osrm_up", False)):
        return _build_straight_path(start_point, ordered_points), "straight"

    stops = [
        {
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "store_code": str(row.get("store_code") or ""),
        }
        for _, row in ordered_points.iterrows()
    ]
    road_geometry = routing_service.fetch_osrm_route_geometry(
        start_lat=float(start_point[0]),
        start_lon=float(start_point[1]),
        ordered_stops=stops,
        runtime_status=runtime_status,
        allow_segment_fallback=False,
    )
    if len(road_geometry) >= 2:
        return [[float(pair[0]), float(pair[1])] for pair in road_geometry], "osrm"
    return _build_straight_path(start_point, ordered_points), "straight"


# Contract: _build_marker_rows converts assignments into marker payload consumed by JS renderer.
def _build_marker_rows(store_points: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for _, row in store_points.iterrows():
        route_order_value = _to_py_scalar(row.get("route_order"))
        store_grade = _normalize_grade(_to_py_scalar(row.get("store_grade")))
        grade_style = _grade_style(store_grade)
        rows.append(
            {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "store_code": str(_to_py_scalar(row.get("store_code")) or ""),
                "store_name": str(_to_py_scalar(row.get("store_name")) or ""),
                "store_grade": store_grade or _DEFAULT_GRADE_STYLE["label"],
                "assignment_status": str(
                    _to_py_scalar(row.get("assignment_status")) or ""
                ),
                "route_order": int(route_order_value)
                if route_order_value is not None
                else None,
                "route_label": f"{int(route_order_value)}"
                if route_order_value is not None
                else "",
                "marker_fill": grade_style["fill"],
                "marker_stroke": grade_style["stroke"],
            }
        )
    return rows


# Contract: _resolve_tile_service_base extracts tile host base from runtime templates.
def _resolve_tile_service_base(runtime_status: dict[str, object]) -> str:
    candidates = [
        str(runtime_status.get("tile_url_template") or "").strip(),
        str(runtime_status.get("vector_tile_template") or "").strip(),
        str(config.MAP_TILE_URL_TEMPLATE or "").strip(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


# Contract: _parse_vector_bounds normalizes runtime vector bounds into [min_lon, min_lat, max_lon, max_lat].
def _parse_vector_bounds(runtime_status: dict[str, object]) -> list[float] | None:
    raw_bounds = runtime_status.get("vector_bounds")
    if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
        return None
    try:
        min_lon = float(raw_bounds[0])
        min_lat = float(raw_bounds[1])
        max_lon = float(raw_bounds[2])
        max_lat = float(raw_bounds[3])
    except (TypeError, ValueError):
        return None
    if min_lon >= max_lon or min_lat >= max_lat:
        return None
    return [min_lon, min_lat, max_lon, max_lat]


# Contract: _is_point_inside_bounds checks whether lat/lon falls inside [min_lon, min_lat, max_lon, max_lat].
def _is_point_inside_bounds(lat: float, lon: float, bounds: list[float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bounds
    return (
        min_lon <= float(lon) <= max_lon and min_lat <= float(lat) <= max_lat
    )


# Contract: _build_vector_fit_coordinates prioritizes points inside vector coverage for fitBounds.
def _build_vector_fit_coordinates(
    marker_rows: list[dict],
    start_point: tuple[float, float] | None,
    runtime_status: dict[str, object],
) -> list[list[float]]:
    all_coords_lon_lat: list[list[float]] = []
    if start_point is not None:
        all_coords_lon_lat.append([float(start_point[1]), float(start_point[0])])
    all_coords_lon_lat.extend(
        [[float(m["lon"]), float(m["lat"])] for m in marker_rows]
    )
    if not all_coords_lon_lat:
        return []

    bounds = _parse_vector_bounds(runtime_status)
    if bounds is None:
        return all_coords_lon_lat

    inside_coords = [
        coord
        for coord in all_coords_lon_lat
        if _is_point_inside_bounds(coord[1], coord[0], bounds)
    ]
    return inside_coords if inside_coords else all_coords_lon_lat


# Contract: _count_markers_outside_vector_bounds reports store markers outside vector tile coverage.
def _count_markers_outside_vector_bounds(
    marker_rows: list[dict],
    runtime_status: dict[str, object],
) -> tuple[int, int]:
    total_count = len(marker_rows)
    if total_count == 0:
        return 0, 0

    bounds = _parse_vector_bounds(runtime_status)
    if bounds is None:
        return 0, total_count

    outside_count = 0
    for marker in marker_rows:
        if not _is_point_inside_bounds(
            float(marker["lat"]),
            float(marker["lon"]),
            bounds,
        ):
            outside_count += 1
    return outside_count, total_count


# Contract: _render_runtime_badge displays one compact status strip for OSRM/Tile health.
def _render_runtime_badge(runtime_status: dict[str, object]) -> None:
    osrm_on = bool(runtime_status.get("osrm_up", False))
    tiles_on = bool(runtime_status.get("tiles_up", False))
    tile_mode = str(runtime_status.get("tile_mode") or "none")
    render_engine = str(runtime_status.get("render_engine") or "leaflet_minimal")
    vector_style_ready = bool(runtime_status.get("vector_style_ready", False))
    vector_dataset_id = str(runtime_status.get("vector_dataset_id") or "—").strip() or "—"
    using_public_raster_fallback = bool(
        runtime_status.get("using_public_raster_fallback", False)
    )
    osrm_latency = runtime_status.get("osrm_latency_ms")
    tiles_latency = runtime_status.get("tiles_latency_ms")
    checked_at = str(runtime_status.get("checked_at") or "—")

    osrm_text = "فعال" if osrm_on else "غیرفعال"
    tile_text = "فعال" if tiles_on else "غیرفعال"
    osrm_color = "#27AE60" if osrm_on else "#E74C3C"
    tile_color = "#27AE60" if tiles_on else "#E74C3C"
    osrm_latency_text = f"{osrm_latency}ms" if osrm_latency is not None else "—"
    tile_latency_text = f"{tiles_latency}ms" if tiles_latency is not None else "—"
    mode_title = {"raster": "Raster", "vector": "Vector", "none": "None"}.get(
        tile_mode, tile_mode
    )
    state_text = str(runtime_status.get("overall_state") or "DOWN")
    engine_text = {
        "maplibre_vector": "MapLibre Vector",
        "leaflet_raster": "Leaflet Raster",
        "leaflet_minimal": "Leaflet Minimal",
    }.get(render_engine, render_engine)
    vector_style_text = "ready" if vector_style_ready else "degraded"

    st.markdown(
        f"""
        <div style="direction:rtl;text-align:right;margin:.15rem 0 .6rem;padding:.55rem .8rem;
                    border-radius:10px;background:#EEF2FF;color:#23395B;font-size:.84rem;">
            <span style="margin-left:1rem;">OSRM: <strong style="color:{osrm_color};">{osrm_text}</strong> ({osrm_latency_text})</span>
            <span style="margin-left:1rem;">Tile: <strong style="color:{tile_color};">{tile_text}</strong> ({tile_latency_text})</span>
            <span style="margin-left:1rem;">Render: <strong>{engine_text}</strong></span>
            <span style="margin-left:1rem;">Vector Style: <strong>{vector_style_text}</strong></span>
            <span style="margin-left:1rem;">Vector Dataset: <strong>{vector_dataset_id}</strong></span>
            <span style="margin-left:1rem;">Raster Fallback: <strong>{'ON' if using_public_raster_fallback else 'OFF'}</strong></span>
            <span style="margin-left:1rem;">حالت Tile: <strong>{mode_title}</strong></span>
            <span style="margin-left:1rem;">وضعیت کلی: <strong>{state_text}</strong></span>
            <span>آخرین بررسی: <strong>{checked_at}</strong></span>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Contract: _render_leaflet_map builds a self-contained iframe map (offline-safe).
def _render_leaflet_map(
    marker_rows: list[dict],
    path_points_lon_lat: list[list[float]],
    start_point: tuple[float, float] | None,
    runtime_status: dict[str, object],
) -> None:
    if not marker_rows:
        st.info("داده‌ای برای نمایش روی نقشه وجود ندارد.")
        return

    leaflet_js = _load_leaflet_js()
    leaflet_css = _load_leaflet_css()
    if not leaflet_js or not leaflet_css:
        st.warning(
            "فایل‌های محلی Leaflet پیدا نشد. پوشه assets/vendor/leaflet را بررسی کنید."
        )
        return

    map_id = f"route-map-{uuid4().hex}"
    lats = [float(m["lat"]) for m in marker_rows]
    lons = [float(m["lon"]) for m in marker_rows]
    if start_point is not None:
        lats.append(float(start_point[0]))
        lons.append(float(start_point[1]))

    payload = {
        "center": [sum(lats) / len(lats), sum(lons) / len(lons)],  # lat, lon
        "zoom": 11,
        "start": [float(start_point[0]), float(start_point[1])]
        if start_point is not None
        else None,
        "markers": marker_rows,
        "path": [
            [float(pair[1]), float(pair[0])] for pair in path_points_lon_lat
        ],  # lat, lon
        "tile_url_template": str(runtime_status.get("tile_url_template") or "").strip(),
        "tile_attribution": str(
            runtime_status.get("public_raster_fallback_attribution")
            or config.MAP_TILE_ATTRIBUTION
            or ""
        ).strip(),
        # FIX: Enable tile layer when any tile URL is available, not only in raster mode.
        # The raster style URL (/styles/basic/{z}/{x}/{y}.png) is often served even when
        # tile_mode is "vector", providing a usable basemap for the Leaflet renderer.
        "tile_enabled": bool(
            str(runtime_status.get("tile_url_template") or "").strip()
        ),
        # FIX: Always include a minimal Tehran outline + district centroids so
        # the operator has city context even when the tile server is down.
        "background_overlay": get_minimal_background_geojson(),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    html_block = f"""
    <style>
      {leaflet_css}
      #{map_id} {{
        width: 100%;
        height: 500px;
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 3px 3px 8px rgba(204, 208, 216, 0.85), -3px -3px 8px rgba(255, 255, 255, 0.95);
      }}
      .bex-stop-icon {{
        background: transparent !important;
        border: none !important;
      }}
    </style>
    <div id="{map_id}"></div>
    <script>{leaflet_js}</script>
    <script>
      (function() {{
        const payload = {payload_json};
        const map = L.map("{map_id}", {{ zoomControl: true }});
        map.setView(payload.center, payload.zoom || 11);

        if (payload.tile_enabled && payload.tile_url_template) {{
          const tileLayer = L.tileLayer(
            payload.tile_url_template,
            {{
              maxNativeZoom: 14,
              maxZoom: 19,
              attribution: payload.tile_attribution || "Local Tiles"
            }}
          );
          tileLayer.on("tileerror", function() {{
            // Keep route and markers visible even if tiles fail.
          }});
          tileLayer.addTo(map);
        }}

        // Minimal Tehran outline + district markers so the map is never a
        // blank canvas when the tile server is down.
        if (payload.background_overlay) {{
          L.geoJSON(payload.background_overlay, {{
            style: function(feature) {{
              if (feature.properties && feature.properties.kind === "city_outline") {{
                return {{
                  color: "#94A3B8",
                  weight: 2,
                  opacity: 0.7,
                  dashArray: "6 6",
                  fill: false
                }};
              }}
              return {{}};
            }},
            pointToLayer: function(feature, latlng) {{
              return L.circleMarker(latlng, {{
                radius: 3,
                color: "#94A3B8",
                weight: 1,
                fillColor: "#CBD5E1",
                fillOpacity: 0.6
              }});
            }},
            onEachFeature: function(feature, layer) {{
              const label = feature.properties && feature.properties.label;
              if (label) {{
                layer.bindTooltip(label, {{
                  permanent: false,
                  direction: "top",
                  className: "bex-district-tooltip"
                }});
              }}
            }}
          }}).addTo(map);
        }}

        if (Array.isArray(payload.path) && payload.path.length > 1) {{
          L.polyline(payload.path, {{
            color: "#3D5FCC",
            weight: 4,
            opacity: 0.85
          }}).addTo(map);
        }}

        if (Array.isArray(payload.start) && payload.start.length === 2) {{
          L.circleMarker(payload.start, {{
            radius: 8,
            color: "#2563EB",
            fillColor: "#3D5FCC",
            fillOpacity: 1,
            weight: 2
          }}).addTo(map).bindPopup("نقطه شروع ویزیتور");
        }}

        const bounds = [];
        if (Array.isArray(payload.start) && payload.start.length === 2) {{
          bounds.push(payload.start);
        }}

        payload.markers.forEach((m) => {{
          const latlng = [m.lat, m.lon];
          bounds.push(latlng);
          const routeNumber = String(m.route_label || "•");
          const markerHtml =
            '<div style="width:26px;height:26px;border-radius:999px;display:flex;align-items:center;justify-content:center;' +
            'font-size:12px;font-weight:700;color:#fff;border:2px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.35);' +
            'background:' + String(m.marker_fill || "#6B7280") + ';">' + routeNumber + "</div>";
          const marker = L.marker(latlng, {{
            icon: L.divIcon({{
              className: "bex-stop-icon",
              html: markerHtml,
              iconSize: [26, 26],
              iconAnchor: [13, 13]
            }})
          }}).addTo(map);

          const routeOrderText = m.route_order === null || m.route_order === undefined ? "—" : String(m.route_order);
          const popupHtml =
            '<div style="direction:rtl;text-align:right;font-family:IRANSansFaNum,IRANSans FaNum,Vazirmatn,IRAN Sans,Tahoma,Arial,sans-serif;line-height:1.8;">' +
            '<b>فروشگاه:</b> ' + String(m.store_code || "") + ' — ' + String(m.store_name || "") + '<br/>' +
            '<b>گرید:</b> ' + String(m.store_grade || "نامشخص") + '<br/>' +
            '<b>ترتیب مسیر:</b> ' + routeOrderText + '<br/>' +
            '<b>وضعیت:</b> ' + String(m.assignment_status || "—") +
            '</div>';
          marker.bindPopup(popupHtml);
        }});

        if (bounds.length > 1) {{
          map.fitBounds(bounds, {{ padding: [30, 30] }});
        }}
      }})();
    </script>
    """
    components.html(html_block, height=520)


# Contract: _render_maplibre_vector_map renders vector-only local tiles as a true basemap.
def _render_maplibre_vector_map(
    marker_rows: list[dict],
    path_points_lon_lat: list[list[float]],
    start_point: tuple[float, float] | None,
    runtime_status: dict[str, object],
) -> tuple[str, str | None]:
    if not marker_rows:
        st.info("داده\u200cای برای نمایش روی نقشه وجود ندارد.")
        return "none", "no_markers"

    tile_base = _resolve_tile_service_base(runtime_status)
    vector_tile_template = str(runtime_status.get("vector_tile_template") or "").strip()
    vector_dataset_id = str(runtime_status.get("vector_dataset_id") or "").strip()
    vector_bounds = _parse_vector_bounds(runtime_status)
    vector_layers_available = [
        str(layer).strip()
        for layer in (runtime_status.get("vector_layers_available") or [])
        if str(layer).strip()
    ]
    vector_style_ready = bool(runtime_status.get("vector_style_ready", False))

    if not tile_base or not vector_tile_template or not vector_style_ready:
        runtime_status["vector_render_error"] = (
            runtime_status.get("vector_render_error") or "vector_style_unavailable"
        )
        _render_leaflet_map(
            marker_rows, path_points_lon_lat, start_point, runtime_status
        )
        return "leaflet_minimal", str(
            runtime_status.get("vector_render_error") or "vector_style_unavailable"
        )

    map_id = f"route-map-vector-{uuid4().hex}"
    lats = [float(m["lat"]) for m in marker_rows]
    lons = [float(m["lon"]) for m in marker_rows]
    if start_point is not None:
        lats.append(float(start_point[0]))
        lons.append(float(start_point[1]))

    center = [sum(lons) / len(lons), sum(lats) / len(lats)]  # lon, lat
    start_feature = (
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(start_point[1]), float(start_point[0])],
            },
            "properties": {
                "title": "\u0646\u0642\u0637\u0647 \u0634\u0631\u0648\u0639 \u0648\u06cc\u0632\u06cc\u062a\u0648\u0631"
            },
        }
        if start_point is not None
        else None
    )
    route_feature = (
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": path_points_lon_lat},
            "properties": {
                "name": "\u0645\u0633\u06cc\u0631 \u067e\u06cc\u0634\u0646\u0647\u0627\u062f\u06cc"
            },
        }
        if len(path_points_lon_lat) > 1
        else None
    )
    stop_features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(m["lon"]), float(m["lat"])],
            },
            "properties": {
                "store_code": str(m.get("store_code") or ""),
                "store_name": str(m.get("store_name") or ""),
                "store_grade": str(
                    m.get("store_grade") or "\u0646\u0627\u0645\u0634\u062e\u0635"
                ),
                "assignment_status": str(m.get("assignment_status") or "?"),
                "route_order": int(m["route_order"])
                if m.get("route_order") is not None
                else None,
                "route_label": str(m.get("route_label") or ""),
                "marker_fill": str(m.get("marker_fill") or "#6B7280"),
                "marker_stroke": str(m.get("marker_stroke") or "#374151"),
            },
        }
        for m in marker_rows
    ]
    fit_coordinates = _build_vector_fit_coordinates(
        marker_rows=marker_rows,
        start_point=start_point,
        runtime_status=runtime_status,
    )

    payload = {
        "map_id": map_id,
        "tile_base": tile_base,
        "center": center,  # lon, lat
        "zoom": 11,
        "vector_tile_template": vector_tile_template,
        "vector_dataset_id": vector_dataset_id or None,
        "vector_bounds": vector_bounds,
        "vector_layers_available": vector_layers_available,
        "fit_coordinates": fit_coordinates,
        "fonts_enabled": bool(runtime_status.get("fonts_available", False)),
        "start_feature": start_feature,
        "route_feature": route_feature,
        "stops_feature_collection": {
            "type": "FeatureCollection",
            "features": stop_features,
        },
        # FIX: Pass raster tile URL so Leaflet fallback can show basemap tiles.
        "tile_url_template": str(runtime_status.get("tile_url_template") or "").strip(),
        "tile_attribution": str(config.MAP_TILE_ATTRIBUTION or "").strip(),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    leaflet_js = _load_leaflet_js()
    leaflet_css = _load_leaflet_css()
    leaflet_js_json = json.dumps(leaflet_js)
    leaflet_css_json = json.dumps(leaflet_css)

    html_block = f"""
    <link rel="stylesheet" href="{tile_base}/maplibre-gl.css" />
    <script src="{tile_base}/maplibre-gl.js"></script>
    <style>
      #{map_id} {{
        width: 100%;
        height: 500px;
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 3px 3px 8px rgba(204, 208, 216, 0.85), -3px -3px 8px rgba(255, 255, 255, 0.95);
      }}
      #{map_id} .maplibregl-popup-content {{
        direction: rtl;
        text-align: right;
        font-family: IRANSansFaNum, IRANSans FaNum, Vazirmatn, IRAN Sans, Tahoma, Arial, sans-serif;
      }}
      #{map_id}.leaflet-fallback-map {{
        background: #ECEFF3;
      }}
      #{map_id} .bex-stop-icon {{
        background: transparent !important;
        border: none !important;
      }}
    </style>
    <div id="{map_id}"></div>
    <script>
      (function() {{
        const payload = {payload_json};
        const leafletJsText = {leaflet_js_json};
        const leafletCssText = {leaflet_css_json};
        const hasLeafletBundle = Boolean(leafletJsText) && Boolean(leafletCssText);
        const container = document.getElementById(payload.map_id);
        let map = null;
        let overlaysReady = false;
        let overlayAttempts = 0;
        let fallbackRendered = false;
        let popupHandlersBound = false;
        const maxOverlayAttempts = 8;

        function ensureLeafletRuntime() {{
          if (!hasLeafletBundle) return false;
          try {{
            if (!document.getElementById(payload.map_id + "-leaflet-css")) {{
              const cssEl = document.createElement("style");
              cssEl.id = payload.map_id + "-leaflet-css";
              cssEl.textContent = leafletCssText;
              document.head.appendChild(cssEl);
            }}
            if (!window.L) {{
              const scriptEl = document.createElement("script");
              scriptEl.text = leafletJsText;
              document.body.appendChild(scriptEl);
            }}
            return Boolean(window.L);
          }} catch (_err) {{
            return false;
          }}
        }}

        function renderLeafletFallback(reason) {{
          if (fallbackRendered) return;
          fallbackRendered = true;
          try {{
            if (map && typeof map.remove === "function") map.remove();
          }} catch (_ignored) {{}}

          if (!container) return;

          if (!ensureLeafletRuntime()) {{
            container.innerHTML = '<div style="padding:1rem;direction:rtl;text-align:right;color:#6B7280;">Basemap در دسترس نیست. مارکرها و مسیر بدون نقشه پایه نمایش داده می\u200cشوند.</div>';
            return;
          }}

          container.classList.add("leaflet-fallback-map");
          const fallbackMap = window.L.map(payload.map_id, {{ zoomControl: true }});
          fallbackMap.setView([payload.center[1], payload.center[0]], payload.zoom || 11);

          // FIX: Add raster tile layer in Leaflet fallback so basemap streets are visible.
          if (payload.tile_url_template) {{
            const tileLayer = window.L.tileLayer(
              payload.tile_url_template,
              {{ maxNativeZoom: 14, maxZoom: 19, attribution: payload.tile_attribution || "Local Tiles" }}
            );
            tileLayer.on("tileerror", function() {{ /* Keep route visible even if tiles fail */ }});
            tileLayer.addTo(fallbackMap);
          }}

          const routeLatLon = [];
          if (payload.route_feature && payload.route_feature.geometry && Array.isArray(payload.route_feature.geometry.coordinates)) {{
            payload.route_feature.geometry.coordinates.forEach(function(pair) {{
              if (Array.isArray(pair) && pair.length >= 2) {{
                routeLatLon.push([pair[1], pair[0]]);
              }}
            }});
          }}
          if (routeLatLon.length > 1) {{
            window.L.polyline(routeLatLon, {{ color: "#3D5FCC", weight: 4, opacity: 0.85 }}).addTo(fallbackMap);
          }}

          if (payload.start_feature && payload.start_feature.geometry && Array.isArray(payload.start_feature.geometry.coordinates)) {{
            const s = payload.start_feature.geometry.coordinates;
            window.L.circleMarker([s[1], s[0]], {{
              radius: 8,
              color: "#2563EB",
              fillColor: "#3D5FCC",
              fillOpacity: 1,
              weight: 2
            }}).addTo(fallbackMap).bindPopup("\u0646\u0642\u0637\u0647 \u0634\u0631\u0648\u0639 \u0648\u06cc\u0632\u06cc\u062a\u0648\u0631");
          }}

          const bounds = [];
          if (payload.start_feature && payload.start_feature.geometry && Array.isArray(payload.start_feature.geometry.coordinates)) {{
            bounds.push([payload.start_feature.geometry.coordinates[1], payload.start_feature.geometry.coordinates[0]]);
          }}

          payload.stops_feature_collection.features.forEach(function(feature) {{
            if (!feature || !feature.geometry || !Array.isArray(feature.geometry.coordinates)) return;
            const coords = feature.geometry.coordinates;
            const p = feature.properties || {{}};
            const routeLabel = String(p.route_label || "?");
            const markerHtml =
              '<div style="width:26px;height:26px;border-radius:999px;display:flex;align-items:center;justify-content:center;' +
              'font-size:12px;font-weight:700;color:#fff;border:2px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.35);' +
              'background:' + String(p.marker_fill || "#6B7280") + ';">' + routeLabel + '</div>';
            const marker = window.L.marker([coords[1], coords[0]], {{
              icon: window.L.divIcon({{
                className: "bex-stop-icon",
                html: markerHtml,
                iconSize: [26, 26],
                iconAnchor: [13, 13]
              }})
            }}).addTo(fallbackMap);
            const routeOrder = p.route_order !== null && p.route_order !== undefined ? String(p.route_order) : "?";
            marker.bindPopup(
              '<div style="direction:rtl;text-align:right;font-family:IRANSansFaNum,IRANSans FaNum,Vazirmatn,IRAN Sans,Tahoma,Arial,sans-serif;line-height:1.8;">' +
              '<b>\u0641\u0631\u0648\u0634\u06af\u0627\u0647:</b> ' + String(p.store_code || "") + ' \u2014 ' + String(p.store_name || "") + '<br/>' +
              '<b>\u06af\u0631\u06cc\u062f:</b> ' + String(p.store_grade || "\u0646\u0627\u0645\u0634\u062e\u0635") + '<br/>' +
              '<b>\u062a\u0631\u062a\u06cc\u0628 \u0645\u0633\u06cc\u0631:</b> ' + routeOrder + '<br/>' +
              '<b>\u0648\u0636\u0639\u06cc\u062a:</b> ' + String(p.assignment_status || "\u2014") +
              '</div>'
            );
            bounds.push([coords[1], coords[0]]);
          }});

          if (bounds.length > 1) {{
            fallbackMap.fitBounds(bounds, {{ padding: [30, 30], maxZoom: 14 }});
          }}
        }}

        if (!window.maplibregl) {{
          renderLeafletFallback("maplibre_missing");
          return;
        }}

        const availableLayers = new Set(Array.isArray(payload.vector_layers_available) ? payload.vector_layers_available : []);
        const hasLayer = function(layerId) {{
          return availableLayers.size === 0 || availableLayers.has(layerId);
        }};

        const styleLayers = [
          {{ id: "bg", type: "background", paint: {{ "background-color": "#ECEFF3" }} }}
        ];
        if (hasLayer("land")) {{
          styleLayers.push({{ id: "land", type: "fill", source: "bex_vector", "source-layer": "land", paint: {{ "fill-color": "#ECE8DE" }} }});
        }}
        if (hasLayer("water_polygons")) {{
          styleLayers.push({{ id: "water", type: "fill", source: "bex_vector", "source-layer": "water_polygons", paint: {{ "fill-color": "#CFE0F4" }} }});
        }}
        if (hasLayer("street_polygons")) {{
          styleLayers.push({{ id: "street_polygons", type: "fill", source: "bex_vector", "source-layer": "street_polygons", paint: {{ "fill-color": "#E0D6C3", "fill-opacity": 0.75 }} }});
        }}
        if (hasLayer("streets")) {{
          styleLayers.push({{ id: "streets", type: "line", source: "bex_vector", "source-layer": "streets", paint: {{ "line-color": "#B8874E", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 14, 2.2], "line-opacity": 0.9 }} }});
        }}
        if (payload.fonts_enabled && hasLayer("street_labels")) {{
          styleLayers.push({{ id: "street_labels", type: "symbol", source: "bex_vector", "source-layer": "street_labels", layout: {{ "text-field": ["coalesce", ["get", "name"], ["get", "name_en"], ["get", "name_de"]], "text-size": 11 }}, paint: {{ "text-color": "#3E4A5A", "text-halo-color": "#FFFFFF", "text-halo-width": 1 }} }});
        }}
        if (payload.fonts_enabled && hasLayer("place_labels")) {{
          styleLayers.push({{ id: "place_labels", type: "symbol", source: "bex_vector", "source-layer": "place_labels", layout: {{ "text-field": ["coalesce", ["get", "name"], ["get", "name_en"], ["get", "name_de"]], "text-size": 12 }}, paint: {{ "text-color": "#2D3B4F", "text-halo-color": "#FFFFFF", "text-halo-width": 1 }} }});
        }}

        const style = {{
          version: 8,
          glyphs: payload.fonts_enabled ? (payload.tile_base + "/fonts/{{fontstack}}/{{range}}.pbf") : undefined,
          sources: {{
            bex_vector: {{
              type: "vector",
              tiles: [payload.vector_tile_template],
              minzoom: 0,
              maxzoom: 14
            }}
          }},
          layers: styleLayers
        }};

        try {{
          map = new maplibregl.Map({{
            container: payload.map_id,
            style: style,
            center: payload.center,
            zoom: payload.zoom || 11
          }});
        }} catch (_initErr) {{
          renderLeafletFallback("map_init_failed");
          return;
        }}

        map.addControl(new maplibregl.NavigationControl(), "top-left");

        function scheduleOverlayRetry(reason) {{
          if (overlaysReady) return;
          overlayAttempts += 1;
          if (overlayAttempts > maxOverlayAttempts) {{
            renderLeafletFallback(reason || "overlay_attach_failed");
            return;
          }}
          const delayMs = Math.min(900, 120 * overlayAttempts);
          setTimeout(attachOverlays, delayMs);
        }}

        function attachOverlays() {{
          if (overlaysReady) return;
          if (!map || !map.isStyleLoaded()) {{
            scheduleOverlayRetry("style_not_loaded");
            return;
          }}
          try {{
            if (payload.route_feature && !map.getSource("bex-route")) {{
              map.addSource("bex-route", {{ type: "geojson", data: payload.route_feature }});
            }}
            if (payload.route_feature && !map.getLayer("bex-route-line")) {{
              map.addLayer({{ id: "bex-route-line", type: "line", source: "bex-route", paint: {{ "line-color": "#3D5FCC", "line-width": 4, "line-opacity": 0.9 }} }});
            }}

            if (payload.start_feature && !map.getSource("bex-start")) {{
              map.addSource("bex-start", {{ type: "geojson", data: {{ type: "FeatureCollection", features: [payload.start_feature] }} }});
            }}
            if (payload.start_feature && !map.getLayer("bex-start-circle")) {{
              map.addLayer({{ id: "bex-start-circle", type: "circle", source: "bex-start", paint: {{ "circle-color": "#3D5FCC", "circle-radius": 7, "circle-stroke-color": "#FFFFFF", "circle-stroke-width": 2 }} }});
            }}

            if (!map.getSource("bex-stops")) {{
              map.addSource("bex-stops", {{ type: "geojson", data: payload.stops_feature_collection }});
            }}
            if (!map.getLayer("bex-stop-circles")) {{
              map.addLayer({{ id: "bex-stop-circles", type: "circle", source: "bex-stops", paint: {{ "circle-color": ["coalesce", ["get", "marker_fill"], "#6B7280"], "circle-radius": 12, "circle-stroke-color": "#FFFFFF", "circle-stroke-width": 2 }} }});
            }}
            if (payload.fonts_enabled && !map.getLayer("bex-stop-labels")) {{
              map.addLayer({{ id: "bex-stop-labels", type: "symbol", source: "bex-stops", layout: {{ "text-field": ["coalesce", ["get", "route_label"], "?"], "text-size": 11 }}, paint: {{ "text-color": "#FFFFFF" }} }});
            }}

            if (!popupHandlersBound) {{
              popupHandlersBound = true;
              map.on("click", "bex-stop-circles", function(e) {{
                const f = e.features && e.features[0];
                if (!f) return;
                const p = f.properties || {{}};
                const routeOrder = p.route_order ? String(p.route_order) : "?";
                const html =
                  "<div><b>\u0641\u0631\u0648\u0634\u06af\u0627\u0647:</b> " + (p.store_code || "") + " \u2014 " + (p.store_name || "") + "<br/>" +
                  "<b>\u06af\u0631\u06cc\u062f:</b> " + (p.store_grade || "\u0646\u0627\u0645\u0634\u062e\u0635") + "<br/>" +
                  "<b>\u062a\u0631\u062a\u06cc\u0628 \u0645\u0633\u06cc\u0631:</b> " + routeOrder + "<br/>" +
                  "<b>\u0648\u0636\u0639\u06cc\u062a:</b> " + (p.assignment_status || "\u2014") + "</div>";
                new maplibregl.Popup().setLngLat(e.lngLat).setHTML(html).addTo(map);
              }});
              map.on("mouseenter", "bex-stop-circles", function() {{
                map.getCanvas().style.cursor = "pointer";
              }});
              map.on("mouseleave", "bex-stop-circles", function() {{
                map.getCanvas().style.cursor = "";
              }});
            }}

            const fitCoords = Array.isArray(payload.fit_coordinates) ? payload.fit_coordinates : [];
            if (fitCoords.length > 0) {{
              const bounds = new maplibregl.LngLatBounds();
              fitCoords.forEach((coord) => {{
                if (Array.isArray(coord) && coord.length === 2) {{
                  bounds.extend(coord);
                }}
              }});
              if (!bounds.isEmpty()) {{
                map.fitBounds(bounds, {{ padding: 30, maxZoom: 14 }});
              }}
            }}
            overlaysReady = true;
          }} catch (_overlayErr) {{
            scheduleOverlayRetry("overlay_attach_failed");
          }}
        }}

        map.on("load", attachOverlays);
        map.on("styledata", attachOverlays);
        map.on("idle", attachOverlays);
        map.on("error", function() {{
          if (!overlaysReady) {{
            scheduleOverlayRetry("map_error");
          }}
        }});
        setTimeout(attachOverlays, 120);
      }})();
    </script>
    """
    components.html(html_block, height=520)
    return "maplibre_vector", None


# Contract: render_route_map is the public map renderer used by manager/visitor/supervisor pages.
def render_route_map(
    stops_df: pd.DataFrame,
    start_lat: float | None = None,
    start_lon: float | None = None,
    runtime_status: dict[str, object] | None = None,
    show_runtime_messages: bool = True,
) -> None:
    if stops_df.empty:
        st.info("هیچ نقطه مسیری برای نمایش روی نقشه وجود ندارد.")
        return

    if not {"lat", "lon"}.issubset(stops_df.columns):
        st.warning("به دلیل نبود مختصات، نمایش نقشه ممکن نیست.")
        return

    safe_df = stops_df.copy()
    safe_df["lat"] = pd.to_numeric(safe_df["lat"], errors="coerce")
    safe_df["lon"] = pd.to_numeric(safe_df["lon"], errors="coerce")
    if "route_order" in safe_df.columns:
        safe_df["route_order"] = pd.to_numeric(safe_df["route_order"], errors="coerce")

    store_points = safe_df.dropna(subset=["lat", "lon"]).copy()
    if store_points.empty:
        st.warning("مختصات معتبر برای فروشگاه‌های تخصیص‌یافته پیدا نشد.")
        return

    effective_runtime_status = (
        dict(runtime_status)
        if runtime_status is not None
        else runtime_health_service.get_offline_runtime_status()
    )
    _render_runtime_badge(effective_runtime_status)

    start_point = _extract_start_point(store_points, start_lat, start_lon)
    marker_rows = _build_marker_rows(store_points)
    ordered_points = store_points.dropna(subset=["route_order"]).sort_values(
        ["route_order", "store_code"]
        if "store_code" in store_points.columns
        else ["route_order"]
    )
    path_points, geometry_source = _build_route_geometry_path(
        start_point=start_point,
        ordered_points=ordered_points,
        runtime_status=effective_runtime_status,
    )

    # FIX: Use render_engine (resolved by runtime_health_service) instead of raw tile_mode
    # to select the correct map renderer. This respects MAP_RENDER_ENGINE config and
    # vector_style_ready checks that already happened during health probe.
    tile_mode = str(effective_runtime_status.get("tile_mode") or "none")
    render_engine = str(
        effective_runtime_status.get("render_engine") or "leaflet_minimal"
    )
    if render_engine == "maplibre_vector":
        _render_maplibre_vector_map(
            marker_rows=marker_rows,
            path_points_lon_lat=path_points,
            start_point=start_point,
            runtime_status=effective_runtime_status,
        )
    else:
        _render_leaflet_map(
            marker_rows=marker_rows,
            path_points_lon_lat=path_points,
            start_point=start_point,
            runtime_status=effective_runtime_status,
        )

    grade_legend = "".join(
        [
            (
                '<span style="display:inline-flex;align-items:center;gap:.35rem;">'
                f'<span style="width:12px;height:12px;border-radius:999px;display:inline-block;background:{style["fill"]};border:1px solid {style["stroke"]};"></span>'
                f"گرید {grade}"
                "</span>"
            )
            for grade, style in _GRADE_STYLES.items()
        ]
    )
    st.markdown(
        f"""<div style="display:flex;gap:1.2rem;margin-top:0.5rem;font-size:0.82rem;color:#5A6878;flex-wrap:wrap;direction:rtl;">
            <span>نقطه شروع ویزیتور</span>
            <span>مسیر پیشنهادی</span>
            {grade_legend}
        </div>""",
        unsafe_allow_html=True,
    )

    if geometry_source == "osrm":
        basis_text = "مبنای ترسیم مسیر: مسیر جاده‌ای OSRM محلی"
    else:
        basis_text = "مبنای ترسیم مسیر: خط مستقیم بین نقاط (در دسترس نبودن OSRM محلی)"
    st.markdown(
        (
            '<div style="direction:rtl;text-align:right;color:#6B7280;font-size:.85rem;margin-top:.15rem;">'
            f"{basis_text}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )

    if show_runtime_messages and not bool(
        effective_runtime_status.get("osrm_up", False)
    ):
        st.warning("OSRM محلی در دسترس نیست؛ مسیر جاده‌ای بهینه نمایش داده نمی‌شود.")
    if show_runtime_messages and not bool(
        effective_runtime_status.get("tiles_up", False)
    ):
        st.warning("Tile server محلی در دسترس نیست؛ خیابان‌ها نمایش داده نمی‌شوند.")
    if (
        show_runtime_messages
        and tile_mode == "vector"
        and not bool(effective_runtime_status.get("fonts_available", False))
    ):
        st.info(
            "Tile محلی از نوع وکتور خام است؛ نمایش نام خیابان‌ها ممکن است محدود باشد."
        )
    if show_runtime_messages and tile_mode == "vector":
        outside_count, total_count = _count_markers_outside_vector_bounds(
            marker_rows=marker_rows,
            runtime_status=effective_runtime_status,
        )
        if total_count > 0 and outside_count > 0:
            dataset_id = str(
                effective_runtime_status.get("vector_dataset_id") or "current"
            ).strip() or "current"
            outside_ratio = outside_count / total_count
            message = (
                f"پوشش دیتاست وکتور فعلی ({dataset_id}) محدود است: "
                f"{outside_count} از {total_count} نقطه خارج از محدوده MBTiles هستند؛ "
                "برای این نقاط، خیابان‌ها خاکستری/خالی دیده می‌شوند. "
                "برای پوشش کامل‌تر می‌توانید MBTiles ایران (یا محدوده بزرگ‌تر) را جایگزین کنید "
                "و در صورت وجود چند دیتاست، MAP_VECTOR_DATASET_ID را روی دیتاست مناسب بگذارید."
            )
            if outside_ratio >= 0.35:
                st.warning(message)
            else:
                st.info(message)

    if show_runtime_messages and bool(
        effective_runtime_status.get("using_public_raster_fallback", False)
    ):
        st.success(
            "برای نمایش شمایل کامل نقشه (خیابان‌ها/نام معابر)، نقشه پایه رستر فعال شده است."
        )
