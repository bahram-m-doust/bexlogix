from __future__ import annotations

from datetime import date

import pytest

from server.app import config
from server.app.models.daily_assignment import DailyAssignment
from server.app.models.daily_visitor_status import DailyVisitorStatus
from server.app.models.store import Store
from server.app.models.user import User
from server.app.models.visitor_profile import VisitorProfile
from server.app.services import assignment_service, routing_service, runtime_health_service


WORK_DATE = date(2026, 8, 30)


def _seed_quality_world(db) -> None:
    manager = User(username="quality-manager", password_hash="x", role="manager", is_active=True)
    db.add(manager)
    db.flush()

    visitor_ids: list[int] = []
    for index in range(2):
        user = User(
            username=f"quality-visitor-{index}",
            password_hash="x",
            role="visitor",
            is_active=True,
        )
        db.add(user)
        db.flush()
        profile = VisitorProfile(
            user_id=user.id,
            visitor_code=f"QVIS-{index:03d}",
            full_name=f"Quality Visitor {index}",
            default_start_lat=35.70 + index * 0.01,
            default_start_lon=51.30 + index * 0.01,
            default_capacity=2,
            is_active=True,
        )
        db.add(profile)
        db.flush()
        db.add(
            DailyVisitorStatus(
                visitor_id=profile.id,
                work_date=WORK_DATE,
                start_lat=profile.default_start_lat,
                start_lon=profile.default_start_lon,
                capacity=2,
                is_active_today=True,
            )
        )
        visitor_ids.append(profile.id)

    stores: list[Store] = []
    for index in range(4):
        store = Store(
            store_code=f"QSTR-{index:03d}",
            store_name=f"Quality Store {index}",
            region="مرکز",
            lat=35.71 + index * 0.01,
            lon=51.31 + index * 0.01,
            grade="A",
            has_confectionery=True,
            has_oil=False,
            has_pasta=False,
        )
        db.add(store)
        db.flush()
        stores.append(store)

    # Keep the real visitor allocation but make final route order the reverse
    # of the canonical raw store-code order for each visitor.
    assignments = [
        (visitor_ids[0], stores[0], 2),
        (visitor_ids[0], stores[2], 1),
        (visitor_ids[1], stores[1], 2),
        (visitor_ids[1], stores[3], 1),
    ]
    for visitor_id, store, route_order in assignments:
        db.add(
            DailyAssignment(
                work_date=WORK_DATE,
                visitor_id=visitor_id,
                store_id=store.id,
                route_order=route_order,
                route_distance_km=None,
                assignment_status="draft",
                generated_by=manager.id,
            )
        )
    db.commit()


def _enable_osrm(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_health_service,
        "get_offline_runtime_status",
        lambda: {"osrm_up": True},
    )


def _route_summary(**overrides) -> dict:
    summary = {
        "osrm_routed": 2,
        "nn_routed": 0,
        "vroom_routed": 0,
        "fallback_stage": None,
        "fallback_reason": None,
        "solver_reason": None,
        "solver_mode": "territory+osrm",
    }
    summary.update(overrides)
    return summary


def test_quality_uses_unoptimized_deterministic_baseline_and_osrm_for_both_sides(
    db_session, monkeypatch
):
    _seed_quality_world(db_session)
    _enable_osrm(monkeypatch)
    monkeypatch.setattr(config, "ROUTING_QUALITY_TARGET_PCT", 20.0)

    def fail_if_nn_runs(*args, **kwargs):
        raise AssertionError("NearestNeighbor must never run for the quality baseline")

    monkeypatch.setattr(routing_service.NearestNeighborRoutePlanner, "plan_route", fail_if_nn_runs)

    calls: list[dict] = []

    def fake_osrm_distance(*, start_lat, start_lon, ordered_stops, **kwargs):
        codes = [str(stop["store_code"]) for stop in ordered_stops]
        calls.append({"start": (start_lat, start_lon), "codes": codes})
        is_raw_order = codes == sorted(codes)
        total = 50.0 if is_raw_order else 25.0
        return [total * (index + 1) / len(codes) for index in range(len(codes))]

    monkeypatch.setattr(
        routing_service,
        "fetch_osrm_cumulative_distances_km",
        fake_osrm_distance,
    )

    result = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )

    assert result["baseline_definition"] == "same_assignments_store_code_order_unoptimized"
    assert result["distance_source"] == "osrm_route_driving"
    assert result["baseline_km"] == pytest.approx(100.0)
    assert result["optimized_km"] == pytest.approx(50.0)
    assert result["saved_km"] == pytest.approx(50.0)
    assert result["improvement_pct"] == pytest.approx(50.0)
    assert result["comparable"] is True
    assert result["route_build_status"] == "success"
    assert result["quality_target_status"] == "achieved"
    assert result["passes_gate"] is True
    assert len(calls) == 4
    assert all(call["start"][0] is not None and call["start"][1] is not None for call in calls)
    assert calls[0]["codes"] == sorted(calls[0]["codes"])
    assert calls[1]["codes"] == sorted(calls[1]["codes"])
    assert calls[2]["codes"] != sorted(calls[2]["codes"])
    assert calls[3]["codes"] != sorted(calls[3]["codes"])


def test_successful_route_below_target_is_not_a_route_failure(db_session, monkeypatch):
    _seed_quality_world(db_session)
    _enable_osrm(monkeypatch)
    monkeypatch.setattr(config, "ROUTING_QUALITY_TARGET_PCT", 20.0)
    call_number = 0

    def fake_osrm_distance(*, ordered_stops, **kwargs):
        nonlocal call_number
        call_number += 1
        # First two calls are raw baseline (50 km each); next two are final
        # routes (45 km each): 10% improvement, below the 20% target.
        total = 50.0 if call_number <= 2 else 45.0
        return [total * (index + 1) / len(ordered_stops) for index in range(len(ordered_stops))]

    monkeypatch.setattr(
        routing_service,
        "fetch_osrm_cumulative_distances_km",
        fake_osrm_distance,
    )

    result = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )

    assert result["baseline_km"] == pytest.approx(100.0)
    assert result["optimized_km"] == pytest.approx(90.0)
    assert result["saved_km"] == pytest.approx(10.0)
    assert result["improvement_pct"] == pytest.approx(10.0)
    assert result["route_build_status"] == "success"
    assert result["quality_target_status"] == "below_target"
    assert result["passes_gate"] is False


def test_osrm_unavailable_makes_quality_not_comparable_without_haversine_mix(
    db_session, monkeypatch
):
    _seed_quality_world(db_session)
    monkeypatch.setattr(
        runtime_health_service,
        "get_offline_runtime_status",
        lambda: {"osrm_up": False},
    )

    def fail_if_distance_is_requested(**kwargs):
        raise AssertionError("OSRM distance must not be requested while health is down")

    monkeypatch.setattr(
        routing_service,
        "fetch_osrm_cumulative_distances_km",
        fail_if_distance_is_requested,
    )

    result = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(
            osrm_routed=0,
            nn_routed=2,
            fallback_stage="osrm_to_nn",
            fallback_reason="osrm_unavailable",
            solver_mode="territory+nn",
        ),
    )

    assert result["comparable"] is False
    assert result["comparison_error"] == "osrm_unavailable"
    assert result["route_build_status"] == "fallback"
    assert result["quality_target_status"] == "not_comparable"
    assert result["distance_source"] == "osrm_route_driving"


def test_missing_visitor_start_makes_quality_not_comparable(db_session, monkeypatch):
    _seed_quality_world(db_session)
    _enable_osrm(monkeypatch)
    contexts = assignment_service.get_active_visitor_day_contexts(db_session, WORK_DATE)
    contexts[0]["start_lat"] = None
    contexts[0]["start_lon"] = None
    monkeypatch.setattr(
        assignment_service,
        "get_active_visitor_day_contexts",
        lambda db, work_date: contexts,
    )

    result = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )

    assert result["comparable"] is False
    assert result["comparison_error"] == "visitor_start_missing"


def test_zero_baseline_is_not_comparable(db_session, monkeypatch):
    _seed_quality_world(db_session)
    _enable_osrm(monkeypatch)
    monkeypatch.setattr(
        routing_service,
        "fetch_osrm_cumulative_distances_km",
        lambda *, ordered_stops, **kwargs: [0.0 for _ in ordered_stops],
    )

    result = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )

    assert result["comparable"] is False
    assert result["comparison_error"] == "baseline_distance_zero"
    assert result["improvement_pct"] == 0.0


def test_quality_result_is_deterministic(db_session, monkeypatch):
    _seed_quality_world(db_session)
    _enable_osrm(monkeypatch)

    def deterministic_distance(*, ordered_stops, **kwargs):
        codes = [str(stop["store_code"]) for stop in ordered_stops]
        total = 30.0 if codes == sorted(codes) else 20.0
        return [total * (index + 1) / len(codes) for index in range(len(codes))]

    monkeypatch.setattr(
        routing_service,
        "fetch_osrm_cumulative_distances_km",
        deterministic_distance,
    )

    first = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )
    second = assignment_service.evaluate_route_quality(
        db=db_session,
        work_date=WORK_DATE,
        route_summary=_route_summary(),
    )

    assert first == second
