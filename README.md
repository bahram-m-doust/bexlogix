# BexLogix

BexLogix is an offline-first Streamlit application for field-sales operations.

## What It Does
- Manages role-based workflows for `manager`, `supervisor`, `visitor`, and `telesales` users.
- Builds daily store assignments and route ordering for field visitors.
- Tracks visit outcomes (`green`, `yellow`, `red`) and creates telesales follow-ups for red visits.
- Provides operational dashboards, route map visualization, and daily reporting/export.

## Core Daily Process
1. Startup seed loads baseline `login.xlsx` and `stores.xlsx` when the database is empty.
2. Manager uploads `visitors.xlsx` (daily visitor status) and optionally new `stores.xlsx`.
3. System creates draft assignments with offline solver mode (`auto|vroom|legacy`), uses local VROOM+OSRM when available, and falls back to nearest-neighbor if needed.
4. Manager publishes assignments.
5. Visitors submit visit results and notes.
6. Red visits are pushed to telesales follow-up queue.
7. Telesales finalizes follow-up outcomes.

## Data File Contracts (`data/`)
- `login.xlsx`: users and passwords for initial seed.
- `stores.xlsx`: master store list (300 stores in sample).
- `visitors.xlsx`: daily visitor status file (10 visitors in sample).

## Run with Docker (recommended)
1. Prepare the OSRM graph once, if it is not already present:
   - `./scripts/offline_prepare_osrm_tehran.ps1`
2. Build and launch the complete stack:
   - `docker compose up --build -d`
3. Open `http://127.0.0.1:8501`.

The Compose stack runs the Streamlit app, OSRM, VROOM, and the local tile
server. Application data is stored in the persistent `bexlogix-runtime`
volume. Use `docker compose logs -f app` to follow application logs and
`docker compose down` to stop the stack.

## Run locally (development fallback)
1. Create environment and install dependencies:
   - `python -m venv .venv`
   - `./.venv/Scripts/activate`
   - `pip install -r requirements.txt`
2. Launch the local services and Streamlit UI:
   - `python scripts/run.py`

The launcher brings up the offline Docker stack (OSRM + VROOM + tiles)
if Docker is available, waits for the services to become healthy, and
then starts the Streamlit UI. Pass `--no-docker` to skip the stack and
rely on the nearest-neighbor fallback.

If you prefer the legacy two-step flow:
1. `./scripts/offline_up.ps1`
2. `streamlit run client/streamlit_app.py`

For full deployment and recovery guide, see `DEPLOY.md`.

## Notes
- The app is designed to run with no external internet dependency at runtime.
- If local VROOM is unavailable, solver automatically falls back to legacy OSRM/NN pipeline.
- If local OSRM is unavailable, route ordering automatically falls back to internal nearest-neighbor.
- If local tiles are unavailable, map markers and route path still render without basemap tiles.

## KPI Evaluation
- Run offline routing KPI checks across work dates:
  - `python scripts/routing_kpi_eval.py --min-days 20`
- Optional: target specific dates:
  - `python scripts/routing_kpi_eval.py --work-date 2026-04-01 --work-date 2026-04-02`
