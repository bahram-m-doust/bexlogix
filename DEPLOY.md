# DEPLOY (Offline Strict)

This project runs in strict offline mode: no runtime call to public map/routing services.

## 1) Prerequisites
- Docker Desktop is running.
- Python dependencies are installed (`pip install -r requirements.txt`).
- You have:
  - `offline/osrm/data/tehran-latest.osm.pbf` (for one-time OSRM build)
  - at least one `.mbtiles` file under `offline/tiles/data/`

If Docker Hub is blocked in your network, pre-load images manually:

```powershell
docker load -i .\offline\images\osrm-backend.tar
docker load -i .\offline\images\tileserver-gl.tar
```

Optional image override env vars:
- `OSRM_DOCKER_IMAGE`
- `TILE_DOCKER_IMAGE`

## 2) One-time OSRM Preparation (Tehran extract)
Build routing graph once:

```powershell
.\scripts\offline_prepare_osrm_tehran.ps1
```

You can override image explicitly:
```powershell
.\scripts\offline_prepare_osrm_tehran.ps1 -OsrmImage "osrm/osrm-backend:latest"
```

Expected output file:
- `offline/osrm/data/tehran-latest.osrm`

## 3) Build and Start the Complete Stack (single command)
Bring up the Streamlit app, OSRM, VROOM, and tile server:

```powershell
docker compose up --build -d
```

This uses `compose.yaml` and exposes:
- Streamlit -> `127.0.0.1:8501`
- OSRM -> `127.0.0.1:5000`
- VROOM -> `127.0.0.1:3000`
- Tile -> `127.0.0.1:8080`

SQLite data persists in the named Docker volume `bexlogix-runtime`.

## 4) Health Check
Run explicit health check anytime:

```powershell
.\scripts\offline_health.ps1
```

To wait until services become ready:

```powershell
.\scripts\offline_health.ps1 -WaitForReady -AllowTilesDown -MaxWaitSeconds 60
```

## 5) Open Application
Open `http://127.0.0.1:8501` in a browser. Follow logs with
`docker compose logs -f app`.

## 6) Required Environment Variables (defaults already local)
- `OSRM_BASE_URL=http://127.0.0.1:5000`
- `OSRM_TIMEOUT_SECONDS=2.5`
- `MAP_TILE_URL_TEMPLATE=http://127.0.0.1:8080/styles/basic/{z}/{x}/{y}.png` (or any local style URL; runtime can auto-discover via `/styles.json`)
- `MAP_TILE_ATTRIBUTION=Local Tiles`
- `OFFLINE_HEALTH_TIMEOUT_SECONDS=0.2`
- `OFFLINE_HEALTH_TTL_SECONDS=20`
- `OFFLINE_OSRM_GRAPH_PATH=offline/osrm/data/tehran-latest.osrm`
- `OFFLINE_TILES_MB_TILES_GLOB=offline/tiles/data/*.mbtiles`

## 7) 5-Minute Recovery Checklist
1. Check Docker is up: `docker compose ps`
2. Check offline services: `.\scripts\offline_health.ps1`
3. If down, restart stack: `docker compose up -d`
4. Re-run manager pipeline for target date.

## 8) Common Failure Triage
- `OSRM DOWN` + missing graph:
  - run `.\scripts\offline_prepare_osrm_tehran.ps1`
- `Tiles DOWN` + no `.mbtiles`:
  - put valid tile file in `offline/tiles/data/`
- Port collision:
  - free ports `5000` and `8080`, then rerun `.\scripts\offline_up.ps1`

## 9) Fallback Behavior (expected)
- If OSRM is down: route ordering uses nearest-neighbor immediately.
- If Tile is down: map still shows markers + path (no street labels).
- If Tiles are vector-only (for example BBBike Shortbread `.mbtiles`):
  - service is considered healthy,
  - map is rendered in vector mode,
  - street labels may be limited unless local glyph fonts are available.
