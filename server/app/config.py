# Purpose: Runtime configuration registry for BexLogix services.
# Workflow Role: Centralizes environment-driven settings for offline deployment.

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse


_DOCKER_SERVICE_HOSTS = frozenset({"osrm", "vroom", "tiles"})


# Contract: _to_float executes one deterministic step in the workflow.
def _to_float(raw_value: str | None, default: float) -> float:
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


# Contract: _to_bool executes one deterministic step in the workflow.
def _to_bool(raw_value: str | None, default: bool) -> bool:
    if raw_value is None:
        return default
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


# Contract: _is_local_or_private_host executes one deterministic step in the workflow.
def _is_local_or_private_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"} | _DOCKER_SERVICE_HOSTS:
        return True
    try:
        ip_obj = ipaddress.ip_address(normalized)
        return bool(ip_obj.is_private or ip_obj.is_loopback)
    except ValueError:
        return False


# Contract: _offline_only_url executes one deterministic step in the workflow.
def _offline_only_url(raw_value: str | None, default: str) -> str:
    candidate = (raw_value or "").strip()
    if not candidate:
        return default
    parsed = urlparse(candidate)
    if _is_local_or_private_host(parsed.hostname):
        return candidate
    # FIX: [OFFLINE-01] Block internet hosts and fall back to local default.
    return default


# Base URL for OSRM route optimization API (offline-first default).
# Example: http://127.0.0.1:5000
OSRM_BASE_URL = _offline_only_url(os.getenv("OSRM_BASE_URL"), "http://127.0.0.1:5000")

# Timeout (seconds) for OSRM HTTP calls before fallback planner is used.
OSRM_TIMEOUT_SECONDS = _to_float(os.getenv("OSRM_TIMEOUT_SECONDS"), 2.5)

# Base URL for local VROOM optimization API (offline-first default).
# Example: http://127.0.0.1:3000
VROOM_BASE_URL = _offline_only_url(
    os.getenv("VROOM_BASE_URL"),
    "http://127.0.0.1:3000",
)

# Timeout (seconds) for VROOM HTTP calls.
VROOM_TIMEOUT_SECONDS = _to_float(os.getenv("VROOM_TIMEOUT_SECONDS"), 10.0)

# Global solver selection for routing pipeline.
# Allowed: legacy | vroom | auto
ROUTING_SOLVER_MODE = os.getenv("ROUTING_SOLVER_MODE", "auto").strip().lower()
if ROUTING_SOLVER_MODE not in {"legacy", "vroom", "auto"}:
    ROUTING_SOLVER_MODE = "auto"

# Shadow mode computes VROOM output for comparison but keeps legacy as source of truth.
# Retained for backward compatibility; the dead code path was removed.
ROUTING_SHADOW_MODE = _to_bool(os.getenv("ROUTING_SHADOW_MODE"), False)

# Cluster due stores into geographic territories per visitor before routing.
# This produces visually compact per-visitor tours even when visitor start
# points are close to each other (the global VRP can otherwise sprawl tours
# across the whole city). Strongly recommended when start points cluster.
ROUTING_USE_TERRITORY_CLUSTERING = _to_bool(
    os.getenv("ROUTING_USE_TERRITORY_CLUSTERING"), True
)

# Preferred map render engine for offline vector/raster pipelines.
# Allowed: auto | leaflet_minimal | maplibre_vector
MAP_RENDER_ENGINE = os.getenv("MAP_RENDER_ENGINE", "auto").strip().lower()
if MAP_RENDER_ENGINE not in {"auto", "leaflet_minimal", "maplibre_vector"}:
    MAP_RENDER_ENGINE = "auto"

# Optional preferred vector dataset ID when tile server exposes multiple datasets
# via /data.json (for example: tehran, iran).
MAP_VECTOR_DATASET_ID = os.getenv("MAP_VECTOR_DATASET_ID", "").strip()

# Base URL of local tile service (used when MAP_TILE_URL_TEMPLATE is empty and
# vector tile auto-discovery is enabled).
# Example: http://127.0.0.1:8080
MAP_TILE_SERVICE_BASE = _offline_only_url(
    os.getenv("MAP_TILE_SERVICE_BASE"),
    "http://127.0.0.1:8080",
).rstrip("/")

# Local tile URL template used by the map iframe.
# Example: http://127.0.0.1:8080/styles/basic/{z}/{x}/{y}.png
# Set to empty string to skip raster probe and rely on vector tile auto-detection.
_raw_tile_template = os.getenv("MAP_TILE_URL_TEMPLATE")
if _raw_tile_template is not None and _raw_tile_template.strip() == "":
    # Explicit empty value: disable raster tile URL so vector auto-detection is used.
    MAP_TILE_URL_TEMPLATE = ""
else:
    MAP_TILE_URL_TEMPLATE = _offline_only_url(
        _raw_tile_template,
        f"{MAP_TILE_SERVICE_BASE}/styles/basic/{{z}}/{{x}}/{{y}}.png",
    )

# Attribution string for local tile providers.
MAP_TILE_ATTRIBUTION = os.getenv("MAP_TILE_ATTRIBUTION", "Local Tiles").strip()

# Optional internet/public raster fallback used only when local raster tiles are
# unavailable (for example vector-only MBTiles with no styles/fonts).
# Example: https://tile.openstreetmap.org/{z}/{x}/{y}.png
MAP_PUBLIC_RASTER_FALLBACK_URL = os.getenv(
    "MAP_PUBLIC_RASTER_FALLBACK_URL",
    "",
).strip()

MAP_PUBLIC_RASTER_FALLBACK_ATTRIBUTION = os.getenv(
    "MAP_PUBLIC_RASTER_FALLBACK_ATTRIBUTION",
    "© OpenStreetMap contributors",
).strip()

# FIX: [PERF-01] Fail-fast probe timeout for offline runtime dependencies.
OFFLINE_HEALTH_TIMEOUT_SECONDS = _to_float(
    os.getenv("OFFLINE_HEALTH_TIMEOUT_SECONDS"),
    0.2,
)

# FIX: [PERF-01] Short-lived health cache TTL (seconds) to avoid repeated probes per rerun.
OFFLINE_HEALTH_TTL_SECONDS = _to_float(
    os.getenv("OFFLINE_HEALTH_TTL_SECONDS"),
    20.0,
)

# Local OSRM graph path used by operational checks/scripts.
OFFLINE_OSRM_GRAPH_PATH = os.getenv(
    "OFFLINE_OSRM_GRAPH_PATH",
    "offline/osrm/data/tehran-latest.osrm",
).strip()

# Local MBTiles glob pattern used by operational checks/scripts.
OFFLINE_TILES_MB_TILES_GLOB = os.getenv(
    "OFFLINE_TILES_MB_TILES_GLOB",
    "offline/tiles/data/*.mbtiles",
).strip()
