# Purpose: Python module in BexLogix project.
# Workflow Role: Supports operational planning and execution flow.

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from server.app import config
from server.app.enums.assignment_status import AssignmentStatus
from server.app.enums.roles import UserRole
from server.app.errors import (
    DomainError,
    PermissionError as AppPermissionError,
    ValidationError,
    err,
)
from server.app.models.daily_assignment import DailyAssignment
from server.app.models.store import Store
from server.app.models.store_schedule_state import StoreScheduleState
from server.app.repositories import (
    assignment_repository,
    store_repository,
    telesales_followup_repository,
    user_repository,
    visit_repository,
    visitor_repository,
)
from server.app.services import (
    routing_service,
    runtime_health_service,
    scheduling_service,
    territory_service,
)
from server.app.utils.geo import haversine_km

logger = logging.getLogger(__name__)

ROUTE_QUALITY_BASELINE_DEFINITION = (
    "same_assignments_store_code_order_unoptimized"
)
ROUTE_QUALITY_DISTANCE_SOURCE = "osrm_route_driving"


# Contract: _assert_manager_user executes one deterministic step in the workflow.
def _assert_manager_user(db: Session, manager_user_id: int) -> None:
    user = user_repository.get_user_by_id(db, manager_user_id)
    if not user:
        raise ValidationError(err("manager_user_not_found"))
    if user.role != UserRole.MANAGER.value:
        raise AppPermissionError(err("manager_only_generate_publish"))


def _assert_supervisor_user(db: Session, supervisor_user_id: int) -> None:
    # FIX: [UX-10] Explicit permission boundary for supervisor approval workflow.
    user = user_repository.get_user_by_id(db, supervisor_user_id)
    if not user:
        raise ValidationError(err("supervisor_user_not_found"))
    if user.role != UserRole.SUPERVISOR.value:
        raise AppPermissionError(err("supervisor_only_approve"))


# Backward-compatible alias for the shared utility.
_haversine_km = haversine_km


# Contract: get_active_visitor_day_contexts executes one deterministic step in the workflow.
def get_active_visitor_day_contexts(db: Session, work_date: date) -> list[dict]:
    rows = visitor_repository.list_active_day_context_rows(db=db, work_date=work_date)

    contexts: list[dict] = []
    for row in rows:
        if not row.user_is_active or not row.profile_is_active:
            continue

        is_active_today = (
            True if row.is_active_today is None else bool(row.is_active_today)
        )
        if not is_active_today:
            continue

        capacity_value = (
            row.capacity
            if row.capacity is not None
            else (row.default_capacity if row.default_capacity is not None else 30)
        )
        capacity_value = int(capacity_value)
        if capacity_value <= 0:
            continue

        start_lat = (
            row.start_lat if row.start_lat is not None else row.default_start_lat
        )
        start_lon = (
            row.start_lon if row.start_lon is not None else row.default_start_lon
        )

        contexts.append(
            {
                "visitor_id": row.visitor_id,
                "visitor_code": row.visitor_code,
                "start_lat": start_lat,
                "start_lon": start_lon,
                "capacity": capacity_value,
            }
        )

    contexts.sort(key=lambda item: item["visitor_code"])
    return contexts


# Contract: _get_due_stores_sorted executes one deterministic step in the workflow.
def _get_due_stores_sorted(db: Session, work_date: date) -> list[Store]:
    due_store_ids = scheduling_service.get_due_store_ids_readonly(db, work_date)
    if not due_store_ids:
        return []

    stores = store_repository.list_stores_by_ids(db=db, store_ids=due_store_ids)
    states = store_repository.list_schedule_states_by_store_ids(
        db=db, store_ids=due_store_ids
    )
    states_by_store_id = {state.store_id: state for state in states}

    # Contract: sort_key executes one deterministic step in the workflow.
    def sort_key(store: Store):
        state = states_by_store_id.get(store.id)
        overdue_days = state.overdue_days if state else 0
        next_visit_date = (
            state.next_visit_date if state and state.next_visit_date else date.min
        )
        interval_days = scheduling_service.get_store_visit_interval_days(store)
        return (-overdue_days, interval_days, next_visit_date, store.store_code)

    return sorted(stores, key=sort_key)


# Contract: _delete_existing_drafts executes one deterministic step in the workflow.
def _delete_existing_drafts(db: Session, work_date: date) -> int:
    return assignment_repository.delete_draft_assignments_for_date(
        db=db, work_date=work_date
    )


# Contract: _select_closest_store_for_visitor executes one deterministic step in the workflow.
def _select_closest_store_for_visitor(
    visitor_state: dict,
    unassigned_stores: list[Store],
    store_priority: dict[int, int],
) -> tuple[Store, float] | None:
    current_lat = visitor_state.get("current_lat")
    current_lon = visitor_state.get("current_lon")
    if current_lat is None or current_lon is None:
        return None

    nearest_store = min(
        unassigned_stores,
        key=lambda store: (
            _haversine_km(current_lat, current_lon, float(store.lat), float(store.lon))
            + (store_priority.get(store.id, 0) * 0.000001)
        ),
    )
    distance = _haversine_km(
        current_lat,
        current_lon,
        float(nearest_store.lat),
        float(nearest_store.lon),
    )
    return nearest_store, distance


# Contract: _geo_capacity_allocate_stores executes one deterministic step in the workflow.
def _geo_capacity_allocate_stores(
    visitors: list[dict], due_stores: list[Store]
) -> dict[int, list[Store]]:
    allocations: dict[int, list[Store]] = {
        visitor["visitor_id"]: [] for visitor in visitors
    }
    if not visitors or not due_stores:
        return allocations

    visitor_state = {
        visitor["visitor_id"]: {
            "remaining_capacity": int(visitor["capacity"]),
            "assigned_count": 0,
            "current_lat": (
                float(visitor["start_lat"])
                if visitor["start_lat"] is not None
                else None
            ),
            "current_lon": (
                float(visitor["start_lon"])
                if visitor["start_lon"] is not None
                else None
            ),
        }
        for visitor in visitors
    }

    store_priority = {store.id: index for index, store in enumerate(due_stores)}
    unassigned_stores = due_stores.copy()

    while unassigned_stores:
        active_visitor_ids = [
            visitor["visitor_id"]
            for visitor in visitors
            if visitor_state[visitor["visitor_id"]]["remaining_capacity"] > 0
        ]
        if not active_visitor_ids:
            break

        candidate_pairs: list[tuple[float, int, Store]] = []
        for visitor in visitors:
            visitor_id = visitor["visitor_id"]
            state = visitor_state[visitor_id]
            if state["remaining_capacity"] <= 0:
                continue
            nearest = _select_closest_store_for_visitor(
                state, unassigned_stores, store_priority
            )
            if nearest is None:
                continue
            nearest_store, distance = nearest
            candidate_pairs.append((distance, visitor_id, nearest_store))

        if candidate_pairs:
            candidate_pairs.sort(
                key=lambda item: (item[0], item[1], item[2].store_code)
            )
            _, selected_visitor_id, selected_store = candidate_pairs[0]
        else:
            # Fallback when start points are missing: assign by smallest load.
            selected_visitor_id = min(
                active_visitor_ids,
                key=lambda visitor_id: (
                    visitor_state[visitor_id]["assigned_count"],
                    visitor_id,
                ),
            )
            selected_store = unassigned_stores[0]

        allocations[selected_visitor_id].append(selected_store)
        visitor_state[selected_visitor_id]["remaining_capacity"] -= 1
        visitor_state[selected_visitor_id]["assigned_count"] += 1
        visitor_state[selected_visitor_id]["current_lat"] = float(selected_store.lat)
        visitor_state[selected_visitor_id]["current_lon"] = float(selected_store.lon)
        unassigned_stores.remove(selected_store)

    return allocations


def _territory_cluster_allocate_stores(
    visitors: list[dict], due_stores: list[Store]
) -> dict[int, list[Store]]:
    """Pre-cluster due stores into per-visitor territories using capacitated
    k-means, then route each visitor's small cluster independently.

    Replaces the global VROOM call when ROUTING_USE_TERRITORY_CLUSTERING is
    on. Output is the same shape as _geo_capacity_allocate_stores so the
    caller can downstream-route uniformly.
    """
    allocations: dict[int, list[Store]] = {int(v["visitor_id"]): [] for v in visitors}
    if not visitors or not due_stores:
        return allocations

    store_by_id = {int(s.id): s for s in due_stores}
    cluster_input_stores = [
        {"store_id": int(s.id), "lat": float(s.lat), "lon": float(s.lon)}
        for s in due_stores
    ]
    territories = territory_service.cluster_stores_to_visitors(
        visitors=visitors, stores=cluster_input_stores
    )
    for t in territories:
        ordered_store_ids = list(t.store_ids)
        allocations[int(t.visitor_id)] = [
            store_by_id[sid] for sid in ordered_store_ids if sid in store_by_id
        ]
    return allocations


# Contract: generate_draft_assignments executes one deterministic step in the workflow.
def generate_draft_assignments(
    db: Session,
    work_date: date,
    manager_user_id: int,
    replace_existing_draft: bool = True,
) -> dict:
    _assert_manager_user(db, manager_user_id)
    scheduling_service.prepare_schedule_state_for_date(
        db=db, target_date=work_date
    )  # FIX: [ARCH-03] Move schedule-state writes to manager pipeline path.

    existing_non_draft_count = (
        assignment_repository.count_non_draft_assignments_for_date(
            db=db,
            work_date=work_date,
        )
    )
    if existing_non_draft_count > 0:
        raise DomainError(err("non_draft_exists_block_regen"))

    if replace_existing_draft:
        _delete_existing_drafts(db, work_date)
    else:
        existing_draft_count = assignment_repository.count_draft_assignments_for_date(
            db=db,
            work_date=work_date,
        )
        if existing_draft_count > 0:
            raise DomainError(err("draft_exists_for_date"))

    visitors = get_active_visitor_day_contexts(db, work_date)
    due_stores = _get_due_stores_sorted(db, work_date)

    solver_mode_config = str(config.ROUTING_SOLVER_MODE or "auto").strip().lower()
    logger.info(
        "assignment_pipeline.start work_date=%s active_visitors=%d due_stores=%d solver=%s",
        work_date.isoformat(),
        len(visitors),
        len(due_stores),
        solver_mode_config,
    )

    store_by_id = {int(store.id): store for store in due_stores}
    solver_store_rows = [
        {
            "store_id": int(store.id),
            "store_code": str(store.store_code),
            "lat": float(store.lat),
            "lon": float(store.lon),
        }
        for store in due_stores
    ]

    use_territory_clustering = bool(
        getattr(config, "ROUTING_USE_TERRITORY_CLUSTERING", False)
        and visitors
        and due_stores
    )

    vroom_result: routing_service.UnifiedRouteResult | None = None
    if (
        not use_territory_clustering
        and solver_mode_config in {"auto", "vroom"}
        and visitors
        and due_stores
    ):
        vroom_result = routing_service.solve_day_assignments_with_vroom(
            visitors=visitors,
            stores=solver_store_rows,
            base_url=config.VROOM_BASE_URL,
            timeout_seconds=config.VROOM_TIMEOUT_SECONDS,
        )

    created_count = 0
    assigned_store_ids: set[int] = set()
    unassigned_store_ids: list[int] = []
    routes_precomputed = False
    solver_mode = "legacy"
    fallback_stage = None
    solver_reason = None
    route_summary: dict | None = None

    use_vroom_output = bool(
        not use_territory_clustering
        and vroom_result
        and vroom_result.solver_mode == "vroom"
    )

    if use_territory_clustering:
        allocations = _territory_cluster_allocate_stores(
            visitors=visitors, due_stores=due_stores
        )
        planner = routing_service.OSRMRoutePlanner()
        osrm_routed = 0
        nn_routed = 0
        fallback_reason_counts = {
            "osrm_unavailable": 0,
            "osrm_timeout": 0,
            "osrm_invalid_response": 0,
        }
        try:
            for visitor in visitors:
                visitor_id = int(visitor["visitor_id"])
                visitor_stores = allocations.get(visitor_id, [])
                if not visitor_stores:
                    continue
                stops = [
                    {
                        "assignment_id": i + 1,
                        "store_id": int(store.id),
                        "store_code": str(store.store_code),
                        "lat": float(store.lat),
                        "lon": float(store.lon),
                    }
                    for i, store in enumerate(visitor_stores)
                ]
                planned_stops = planner.plan_route(
                    start_lat=visitor.get("start_lat"),
                    start_lon=visitor.get("start_lon"),
                    stops=stops,
                )
                # Map planning result back to store_id via the temp assignment_id.
                store_by_temp_id = {s["assignment_id"]: s for s in stops}
                planned_by_temp_id = {p.assignment_id: p for p in planned_stops}

                for temp_id in sorted(store_by_temp_id.keys()):
                    p = planned_by_temp_id.get(temp_id)
                    if p is None:
                        continue
                    stop = store_by_temp_id[temp_id]
                    db.add(
                        DailyAssignment(
                            work_date=work_date,
                            visitor_id=visitor_id,
                            store_id=int(stop["store_id"]),
                            route_order=int(p.route_order),
                            route_distance_km=p.route_distance_km,
                            assignment_status=AssignmentStatus.DRAFT.value,
                            generated_by=manager_user_id,
                        )
                    )
                    assigned_store_ids.add(int(stop["store_id"]))
                    created_count += 1

                mode = str(getattr(planner, "last_plan_mode", "nn"))
                if mode == "osrm":
                    osrm_routed += 1
                else:
                    nn_routed += 1
                    reason = getattr(planner, "last_fallback_reason", None)
                    if reason in fallback_reason_counts:
                        fallback_reason_counts[reason] += 1
                    elif reason:
                        fallback_reason_counts["osrm_invalid_response"] += 1
            db.commit()
        except Exception:
            db.rollback()
            raise

        unassigned_store_ids = [
            int(s.id) for s in due_stores if int(s.id) not in assigned_store_ids
        ]
        solver_mode = "territory+osrm" if osrm_routed > 0 else "territory+nn"
        routes_precomputed = True
        if nn_routed > 0:
            fallback_stage = "osrm_to_nn"
            solver_reason = max(
                fallback_reason_counts.items(), key=lambda item: item[1]
            )[0]
            if fallback_reason_counts.get(solver_reason, 0) <= 0:
                solver_reason = None
        route_summary = {
            "total_assignments": int(created_count),
            "total_visitors": int(len(visitors)),
            "osrm_routed": int(osrm_routed),
            "nn_routed": int(nn_routed),
            "vroom_routed": 0,
            "vroom_used": False,
            "osrm_used": bool(osrm_routed > 0),
            "solver_mode": solver_mode,
            "fallback_stage": fallback_stage,
            "solver_reason": solver_reason,
            "fallback_reason": solver_reason,
            "fallback_reason_counts": fallback_reason_counts,
            "clustering": "capacitated_kmeans",
        }
    elif use_vroom_output:
        solver_mode = "vroom"
        routes_precomputed = True
        visitor_by_id = {int(visitor["visitor_id"]): visitor for visitor in visitors}
        raw_planned_assignments = sorted(
            vroom_result.assignments,
            key=lambda item: (item.visitor_id, item.route_order, item.store_id),
        )
        # Ensure per-visitor route_order always starts at 1 and stays contiguous.
        planned_assignments: list[routing_service.PlannedAssignment] = []
        by_visitor: dict[int, list[routing_service.PlannedAssignment]] = {}
        for item in raw_planned_assignments:
            store = store_by_id.get(int(item.store_id))
            if store is None:
                continue
            by_visitor.setdefault(int(item.visitor_id), []).append(item)
        for visitor_id in sorted(by_visitor.keys()):
            ordered_rows = sorted(
                by_visitor[visitor_id],
                key=lambda item: (item.route_order, item.store_id),
            )
            visitor_ctx = visitor_by_id.get(int(visitor_id), {})
            start_lat = visitor_ctx.get("start_lat")
            start_lon = visitor_ctx.get("start_lon")
            ordered_stop_rows: list[dict] = []
            for item in ordered_rows:
                store = store_by_id.get(int(item.store_id))
                if store is None:
                    continue
                ordered_stop_rows.append(
                    {
                        "lat": float(store.lat),
                        "lon": float(store.lon),
                        "store_code": str(store.store_code),
                    }
                )
            osrm_cumulative = routing_service.fetch_osrm_cumulative_distances_km(
                start_lat=float(start_lat) if start_lat is not None else None,
                start_lon=float(start_lon) if start_lon is not None else None,
                ordered_stops=ordered_stop_rows,
            )
            # FIX: Build a defensive cumulative list whose length equals the
            # number of ordered stops; never let it be shorter than the consumer
            # expects.
            cumulative_distances: list[float | None]
            if (
                osrm_cumulative is not None
                and len(osrm_cumulative) == len(ordered_stop_rows)
            ):
                cumulative_distances = list(osrm_cumulative)
            elif start_lat is None or start_lon is None:
                cumulative_distances = [None] * len(ordered_stop_rows)
            else:
                ordered_points = [
                    (float(stop["lat"]), float(stop["lon"]))
                    for stop in ordered_stop_rows
                ]
                from server.app.utils.geo import cumulative_path_distance_km

                cumulative_distances = list(
                    cumulative_path_distance_km(
                        float(start_lat), float(start_lon), ordered_points
                    )
                )
                # Pad with None if somehow shorter (should not happen).
                while len(cumulative_distances) < len(ordered_stop_rows):
                    cumulative_distances.append(None)

            for normalized_order, item in enumerate(ordered_rows, start=1):
                index = normalized_order - 1
                if 0 <= index < len(cumulative_distances):
                    distance_km = cumulative_distances[index]
                elif item.route_distance_km is not None:
                    distance_km = float(item.route_distance_km)
                else:
                    distance_km = None
                planned_assignments.append(
                    routing_service.PlannedAssignment(
                        visitor_id=int(item.visitor_id),
                        store_id=int(item.store_id),
                        route_order=int(normalized_order),
                        route_distance_km=distance_km,
                    )
                )
        try:
            for planned in planned_assignments:
                db.add(
                    DailyAssignment(
                        work_date=work_date,
                        visitor_id=int(planned.visitor_id),
                        store_id=int(planned.store_id),
                        route_order=int(planned.route_order),
                        route_distance_km=planned.route_distance_km,
                        assignment_status=AssignmentStatus.DRAFT.value,
                        generated_by=manager_user_id,
                    )
                )
                assigned_store_ids.add(int(planned.store_id))
                created_count += 1
            db.commit()
        except Exception:
            db.rollback()
            raise

        vroom_unassigned = {int(item) for item in vroom_result.unassigned_store_ids}
        unassigned_store_ids = sorted(
            vroom_unassigned.union(set(store_by_id.keys()) - assigned_store_ids)
        )
        vroom_visitor_ids = {
            int(item.visitor_id) for item in planned_assignments if item.route_order > 0
        }
        route_summary = {
            "total_assignments": int(created_count),
            "total_visitors": int(len(visitors)),
            "osrm_routed": 0,
            "nn_routed": 0,
            "vroom_routed": int(len(vroom_visitor_ids)),
            "vroom_used": True,
            "osrm_used": False,
            "solver_mode": "vroom",
            "fallback_stage": None,
            "solver_reason": None,
            "fallback_reason": None,
            "fallback_reason_counts": {
                "osrm_unavailable": 0,
                "osrm_timeout": 0,
                "osrm_invalid_response": 0,
            },
        }
    else:
        if vroom_result:
            fallback_stage = vroom_result.fallback_stage
            solver_reason = vroom_result.solver_reason

        allocations = _geo_capacity_allocate_stores(
            visitors=visitors,
            due_stores=due_stores,
        )
        try:
            for visitor in visitors:
                visitor_id = int(visitor["visitor_id"])
                for store in allocations.get(visitor_id, []):
                    db.add(
                        DailyAssignment(
                            work_date=work_date,
                            visitor_id=visitor_id,
                            store_id=store.id,
                            route_order=None,
                            route_distance_km=None,
                            assignment_status=AssignmentStatus.DRAFT.value,
                            generated_by=manager_user_id,
                        )
                    )
                    assigned_store_ids.add(store.id)
                    created_count += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        unassigned_store_ids = [
            store.id for store in due_stores if store.id not in assigned_store_ids
        ]

    logger.info(
        "assignment_pipeline.done work_date=%s solver_mode=%s created=%d unassigned=%d "
        "routes_precomputed=%s fallback_stage=%s solver_reason=%s",
        work_date.isoformat(),
        solver_mode,
        created_count,
        len(unassigned_store_ids),
        routes_precomputed,
        fallback_stage,
        solver_reason,
    )
    return {
        "work_date": work_date.isoformat(),
        "active_visitors": len(visitors),
        "due_stores": len(due_stores),
        "created_assignments": created_count,
        "unassigned_due_stores": len(unassigned_store_ids),
        "unassigned_store_ids": unassigned_store_ids,
        "routes_precomputed": bool(routes_precomputed),
        "solver_mode": solver_mode,
        "fallback_stage": fallback_stage,
        "solver_reason": solver_reason,
        "route_summary": route_summary,
    }


# Contract: get_unassigned_due_store_ids executes one deterministic step in the workflow.
def get_unassigned_due_store_ids(db: Session, work_date: date) -> list[int]:
    due_store_ids = set(scheduling_service.get_due_store_ids_readonly(db, work_date))
    if not due_store_ids:
        return []

    assigned_store_ids = set(
        assignment_repository.list_assigned_store_ids_for_date(
            db=db, work_date=work_date
        )
    )
    return sorted(due_store_ids - assigned_store_ids)


# Contract: publish_assignments executes one deterministic step in the workflow.
def publish_assignments(
    db: Session,
    work_date: date,
    manager_user_id: int,
    include_draft_override: bool = False,
) -> int:
    # FIX: [UX-10] Publish defaults to supervisor_approved; manager can override to include draft.
    # FIX: [CONCURRENCY] Use a status-guarded UPDATE so concurrent managers
    # cannot double-publish. Each assignment row transitions atomically from
    # its expected source status to PUBLISHED.
    _assert_manager_user(db, manager_user_id)

    publishable_assignments = assignment_repository.list_assignments_for_publish(
        db=db,
        work_date=work_date,
        include_draft_override=include_draft_override,
    )
    if not publishable_assignments:
        raise DomainError(
            err("no_draft_assignments")
            if include_draft_override
            else err("no_supervisor_approved_to_publish")
        )

    expected_source_statuses = [AssignmentStatus.SUPERVISOR_APPROVED.value]
    if include_draft_override:
        expected_source_statuses.append(AssignmentStatus.DRAFT.value)

    publish_time = datetime.now(timezone.utc)
    target_ids = [int(a.id) for a in publishable_assignments]
    try:
        updated_count = (
            db.query(DailyAssignment)
            .filter(
                DailyAssignment.id.in_(target_ids),
                DailyAssignment.assignment_status.in_(expected_source_statuses),
            )
            .update(
                {
                    DailyAssignment.assignment_status: AssignmentStatus.PUBLISHED.value,
                    DailyAssignment.published_at: publish_time,
                },
                synchronize_session=False,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if updated_count == 0:
        # All rows transitioned out from under us between our read and update.
        raise DomainError(err("publish_concurrent_modification"))

    return int(updated_count)


def approve_visitor_route_for_work_date(
    db: Session,
    work_date: date,
    visitor_id: int,
    supervisor_user_id: int,
) -> int:
    # FIX: [UX-10] Supervisor can move draft assignments to supervisor_approved per visitor/date.
    _assert_supervisor_user(db=db, supervisor_user_id=supervisor_user_id)
    draft_assignments = assignment_repository.list_assignments_for_visitor_date(
        db=db,
        work_date=work_date,
        visitor_id=visitor_id,
        status=AssignmentStatus.DRAFT.value,
    )
    if not draft_assignments:
        raise DomainError(err("no_draft_for_visitor_approve"))

    try:
        for assignment in draft_assignments:
            assignment.assignment_status = AssignmentStatus.SUPERVISOR_APPROVED.value
        db.commit()
        return len(draft_assignments)
    except Exception:
        db.rollback()
        raise


# Contract: get_work_date_operational_snapshot executes one deterministic step in the workflow.
def get_work_date_operational_snapshot(db: Session, work_date: date) -> dict:
    assignment_count = assignment_repository.count_assignments_for_date(
        db=db, work_date=work_date
    )
    status_counts = assignment_repository.count_assignments_grouped_by_status(
        db=db,
        work_date=work_date,
    )
    draft_count = int(status_counts.get(AssignmentStatus.DRAFT.value, 0))
    supervisor_approved_count = int(
        status_counts.get(AssignmentStatus.SUPERVISOR_APPROVED.value, 0)
    )
    published_count = int(status_counts.get(AssignmentStatus.PUBLISHED.value, 0))
    completed_count = int(status_counts.get(AssignmentStatus.COMPLETED.value, 0))
    skipped_count = int(status_counts.get(AssignmentStatus.SKIPPED.value, 0))
    non_draft_count = assignment_repository.count_non_draft_assignments_for_date(
        db=db,
        work_date=work_date,
    )
    visit_count = assignment_repository.count_visits_linked_to_assignment_date(
        db=db,
        work_date=work_date,
    )
    followup_count = assignment_repository.count_followups_linked_to_assignment_date(
        db=db,
        work_date=work_date,
    )

    return {
        "work_date": work_date.isoformat(),
        "assignment_count": int(assignment_count),
        "draft_count": draft_count,
        "supervisor_approved_count": supervisor_approved_count,
        "published_count": published_count,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "non_draft_count": int(non_draft_count),
        "visit_count": int(visit_count),
        "followup_count": int(followup_count),
        "is_locked": bool(
            published_count > 0
            or completed_count > 0
            or skipped_count > 0
            or visit_count > 0
            or followup_count > 0
        ),
    }


def _rebuild_store_schedule_states_for_stores(
    db: Session,
    store_ids: list[int],
    baseline_date: date,
) -> None:
    """Replay every visit + telesales event for `store_ids` in memory and
    update each StoreScheduleState row in a single batch.

    Previously this was an O(N · E) pattern (N stores, E events each) with
    per-store and per-event SQL round trips. The hot loop fetched Store and
    StoreScheduleState rows again on every event. The new implementation does
    a fixed number of queries regardless of N:
      1 query for stores, 1 for states, 1 for visits, 1 for followups,
      then pure-Python replay using the apply_*_event_to_state helpers.
    """
    if not store_ids:
        return

    sorted_ids = sorted(set(int(sid) for sid in store_ids))

    stores = store_repository.list_stores_by_ids(db=db, store_ids=sorted_ids)
    stores_by_id = {int(s.id): s for s in stores}

    existing_states = store_repository.list_schedule_states_by_store_ids(
        db=db, store_ids=sorted_ids
    )
    states_by_store_id: dict[int, StoreScheduleState] = {
        int(s.store_id): s for s in existing_states
    }

    # Create missing state rows up front (one flush rather than per-store).
    missing_state_store_ids = [
        sid for sid in sorted_ids if sid not in states_by_store_id
    ]
    for store_id in missing_state_store_ids:
        new_state = StoreScheduleState(
            store_id=store_id,
            next_visit_date=baseline_date,
            overdue_days=0,
            in_telesales_queue=False,
        )
        db.add(new_state)
        states_by_store_id[store_id] = new_state
    if missing_state_store_ids:
        db.flush()

    # Reset every state to its baseline; the replay below will overwrite it
    # if any events exist.
    for store_id in sorted_ids:
        state = states_by_store_id[store_id]
        state.last_visit_date = None
        state.last_visit_result = None
        state.next_visit_date = baseline_date
        state.overdue_days = 0
        state.in_telesales_queue = False

    visits_by_store = visit_repository.list_visit_events_grouped_by_store(
        db=db, store_ids=sorted_ids
    )
    followups_by_store = (
        telesales_followup_repository.list_finalized_followup_events_grouped_by_store(
            db=db, store_ids=sorted_ids
        )
    )

    for store_id in sorted_ids:
        store = stores_by_id.get(store_id)
        if store is None:
            # Store was deleted but state remains; leave at baseline.
            continue
        state = states_by_store_id[store_id]

        events: list[tuple[date, int, str, str]] = []
        for visit_date, result in visits_by_store.get(store_id, []):
            events.append((visit_date, 0, "visit", result))
        for followup_date, result in followups_by_store.get(store_id, []):
            events.append((followup_date, 1, "followup", result))
        events.sort(key=lambda item: (item[0], item[1]))

        for event_date, _ordering, event_type, value in events:
            if event_type == "visit":
                scheduling_service.apply_visit_event_to_state(
                    state=state,
                    store=store,
                    visit_date=event_date,
                    visit_result=value,
                )
            else:
                scheduling_service.apply_telesales_event_to_state(
                    state=state,
                    store=store,
                    followup_date=event_date,
                    outcome=value,
                )


# Backward-compatible single-store wrapper for any external caller.
def _rebuild_store_schedule_state_from_history(
    db: Session,
    store_id: int,
    baseline_date: date,
) -> None:
    _rebuild_store_schedule_states_for_stores(
        db=db, store_ids=[int(store_id)], baseline_date=baseline_date
    )


# Contract: flush_work_date_operational_data executes one deterministic step in the workflow.
def flush_work_date_operational_data(
    db: Session, work_date: date, manager_user_id: int
) -> dict:
    _assert_manager_user(db, manager_user_id)

    assignment_rows = assignment_repository.list_assignment_ids_and_store_ids_for_date(
        db=db,
        work_date=work_date,
    )
    assignment_ids = [int(assignment_id) for assignment_id, _ in assignment_rows]
    affected_store_ids = {int(store_id) for _, store_id in assignment_rows}

    visit_ids = visit_repository.list_visit_ids_by_assignment_ids(
        db=db,
        assignment_ids=assignment_ids,
    )

    followup_deleted = telesales_followup_repository.delete_followups_by_visit_ids(
        db=db,
        visit_ids=visit_ids,
    )

    visits_deleted = visit_repository.delete_visits_by_assignment_ids(
        db=db,
        assignment_ids=assignment_ids,
    )

    assignments_deleted = assignment_repository.delete_assignments_for_date(
        db=db,
        work_date=work_date,
    )

    try:
        db.flush()
        _rebuild_store_schedule_states_for_stores(
            db=db,
            store_ids=sorted(affected_store_ids),
            baseline_date=work_date,
        )
        db.commit()
    except Exception:
        logger.exception(
            "flush_work_date_operational_data.failed work_date=%s manager_user_id=%s "
            "deleted_assignments=%d deleted_visits=%d deleted_followups=%d affected_stores=%d",
            work_date.isoformat(),
            manager_user_id,
            assignments_deleted,
            visits_deleted,
            followup_deleted,
            len(affected_store_ids),
        )
        db.rollback()
        raise

    logger.warning(
        "flush_work_date_operational_data.committed work_date=%s manager_user_id=%s "
        "deleted_assignments=%d deleted_visits=%d deleted_followups=%d affected_stores=%d",
        work_date.isoformat(),
        manager_user_id,
        assignments_deleted,
        visits_deleted,
        followup_deleted,
        len(affected_store_ids),
    )
    return {
        "work_date": work_date.isoformat(),
        "assignments_deleted": int(assignments_deleted),
        "visits_deleted": int(visits_deleted),
        "followups_deleted": int(followup_deleted),
        "affected_stores": len(affected_store_ids),
    }


# Contract: _resolve_route_build_status keeps route execution separate from its quality target.
def _resolve_route_build_status(
    assignment_rows: list,
    route_summary: dict | None,
) -> str:
    if not assignment_rows or any(row.route_order is None for row in assignment_rows):
        return "failed"
    summary = route_summary or {}
    fallback_stage = str(summary.get("fallback_stage") or "").strip()
    solver_mode = str(summary.get("solver_mode") or "").strip().lower()
    if (
        int(summary.get("nn_routed", 0) or 0) > 0
        or bool(fallback_stage)
        or solver_mode.endswith("+nn")
    ):
        return "fallback"
    return "success"


def _empty_route_quality_result(
    work_date: date,
    *,
    route_build_status: str,
    fallback_reason: str | None,
    comparison_error: str,
) -> dict:
    target_pct = float(config.ROUTING_QUALITY_TARGET_PCT)
    return {
        "work_date": work_date.isoformat(),
        "baseline_definition": ROUTE_QUALITY_BASELINE_DEFINITION,
        "distance_source": ROUTE_QUALITY_DISTANCE_SOURCE,
        "baseline_km": 0.0,
        "optimized_km": 0.0,
        # Backward-compatible field used by older dashboard code/exporters.
        "current_km": 0.0,
        "saved_km": 0.0,
        "improvement_pct": 0.0,
        "target_pct": target_pct,
        "comparable": False,
        "route_build_status": route_build_status,
        "quality_target_status": "not_comparable",
        "fallback_reason": fallback_reason,
        "comparison_error": comparison_error,
        "passes_gate": False,
    }


def _fetch_osrm_total_km(
    *,
    rows_by_visitor: dict[int, list[dict]],
    context_by_visitor_id: dict[int, dict],
    order_key,
    runtime_status: dict[str, object],
) -> tuple[float | None, str | None]:
    total_km = 0.0
    for visitor_id in sorted(rows_by_visitor):
        rows = rows_by_visitor[visitor_id]
        if not rows:
            continue
        context = context_by_visitor_id.get(visitor_id)
        start_lat = context.get("start_lat") if context else None
        start_lon = context.get("start_lon") if context else None
        if start_lat is None or start_lon is None:
            return None, "visitor_start_missing"

        ordered_rows = sorted(rows, key=order_key)
        cumulative = routing_service.fetch_osrm_cumulative_distances_km(
            start_lat=float(start_lat),
            start_lon=float(start_lon),
            ordered_stops=ordered_rows,
            runtime_status=runtime_status,
        )
        if cumulative is None or len(cumulative) != len(ordered_rows):
            return None, "osrm_distance_unavailable"
        total_km += float(cumulative[-1])
    return total_km, None


# Contract: evaluate_route_quality compares one raw deterministic order with the final route.
def evaluate_route_quality(
    db: Session,
    work_date: date,
    route_summary: dict | None = None,
) -> dict:
    assignment_rows = (
        db.query(
            DailyAssignment.visitor_id,
            DailyAssignment.route_order,
            Store.id.label("store_id"),
            Store.store_code,
            Store.lat,
            Store.lon,
        )
        .join(Store, Store.id == DailyAssignment.store_id)
        .filter(DailyAssignment.work_date == work_date)
        .all()
    )
    route_build_status = _resolve_route_build_status(
        assignment_rows=assignment_rows,
        route_summary=route_summary,
    )
    fallback_reason = None
    if route_summary:
        fallback_reason = route_summary.get("fallback_reason") or route_summary.get(
            "solver_reason"
        )
    if not assignment_rows:
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error="assignments_missing",
        )
    if route_build_status == "failed":
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error="current_route_order_missing",
        )

    visitor_contexts = get_active_visitor_day_contexts(db, work_date)
    context_by_visitor_id = {int(item["visitor_id"]): item for item in visitor_contexts}

    rows_by_visitor: dict[int, list[dict]] = {}
    for visitor_id, route_order, store_id, store_code, lat, lon in assignment_rows:
        rows_by_visitor.setdefault(int(visitor_id), []).append(
            {
                "route_order": route_order,
                "store_id": int(store_id),
                "store_code": store_code,
                "lat": float(lat),
                "lon": float(lon),
            }
        )

    runtime_status = runtime_health_service.get_offline_runtime_status()
    if not bool(runtime_status.get("osrm_up", False)):
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error="osrm_unavailable",
        )

    # Raw baseline: preserve the real visitor-to-store allocation and rebuild a
    # deterministic, unoptimized order from store_code. No NN/OSRM optimizer is
    # allowed to reorder this side of the comparison.
    baseline_total_km, baseline_error = _fetch_osrm_total_km(
        rows_by_visitor=rows_by_visitor,
        context_by_visitor_id=context_by_visitor_id,
        order_key=lambda row: (str(row["store_code"]), int(row["store_id"])),
        runtime_status=runtime_status,
    )
    if baseline_total_km is None:
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error=baseline_error or "osrm_distance_unavailable",
        )

    optimized_total_km, optimized_error = _fetch_osrm_total_km(
        rows_by_visitor=rows_by_visitor,
        context_by_visitor_id=context_by_visitor_id,
        order_key=lambda row: (
            int(row["route_order"]),
            str(row["store_code"]),
            int(row["store_id"]),
        ),
        runtime_status=runtime_status,
    )
    if optimized_total_km is None:
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error=optimized_error or "osrm_distance_unavailable",
        )

    if baseline_total_km <= 0:
        return _empty_route_quality_result(
            work_date,
            route_build_status=route_build_status,
            fallback_reason=fallback_reason,
            comparison_error="baseline_distance_zero",
        )

    saved_km = baseline_total_km - optimized_total_km
    improvement_pct = (saved_km / baseline_total_km) * 100.0
    target_pct = float(config.ROUTING_QUALITY_TARGET_PCT)
    passes_gate = bool(improvement_pct >= target_pct)

    return {
        "work_date": work_date.isoformat(),
        "baseline_definition": ROUTE_QUALITY_BASELINE_DEFINITION,
        "distance_source": ROUTE_QUALITY_DISTANCE_SOURCE,
        "baseline_km": round(baseline_total_km, 3),
        "optimized_km": round(optimized_total_km, 3),
        "current_km": round(optimized_total_km, 3),
        "saved_km": round(saved_km, 3),
        "improvement_pct": round(improvement_pct, 2),
        "target_pct": target_pct,
        "comparable": True,
        "route_build_status": route_build_status,
        "quality_target_status": "achieved" if passes_gate else "below_target",
        "fallback_reason": fallback_reason,
        "comparison_error": None,
        "passes_gate": passes_gate,
    }


# Backward-compatible API for existing integrations.
def evaluate_route_quality_vs_round_robin(
    db: Session,
    work_date: date,
    route_summary: dict | None = None,
) -> dict:
    return evaluate_route_quality(
        db=db,
        work_date=work_date,
        route_summary=route_summary,
    )
