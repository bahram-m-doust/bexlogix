# Purpose: Manager dashboard with route pipeline, governance controls, and operational monitoring.
# Workflow Role: Main control plane for manager actions in the daily planning lifecycle.

from __future__ import annotations

import os
from datetime import date
from tempfile import NamedTemporaryFile

import pandas as pd
import streamlit as st

from client.components.empty_state import get_empty_state_message
from client.components.jalali_date import jalali_date_input
from client.components.rtl_table import render_rtl_table
from client.components.route_map import render_route_map
from server.app.errors import DomainError
from client.styles.neumorphism import (
    neu_section_header,
    render_metric_grid,
    render_page_title,
)
from server.app.services import (
    assignment_service,
    dashboard_query_service,
    import_service,
    offline_stack_service,
    reporting_export_service,
    routing_service,
    runtime_health_service,
    telesales_service,
    visit_service,
)
from server.app.services.import_daily_visitor_status_service import (
    import_daily_visitor_statuses_from_excel,
)
from server.db.database import get_db


def _save_uploaded_excel(uploaded_file) -> str | None:
    if uploaded_file is None:
        return None
    with NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name


def _fallback_reason_fa(reason: str | None) -> str:
    if reason == "vroom_timeout":
        return "پاسخ VROOM در زمان مقرر دریافت نشد."
    if reason == "vroom_invalid_response":
        return "پاسخ VROOM معتبر نبود."
    if reason == "vroom_unavailable":
        return "سرویس VROOM محلی در دسترس نبود."
    if reason == "osrm_timeout":
        return "پاسخ OSRM در زمان مقرر دریافت نشد."
    if reason == "osrm_invalid_response":
        return "پاسخ OSRM معتبر نبود."
    if reason == "osrm_unavailable":
        return "سرویس OSRM محلی در دسترس نبود."
    if reason == "osrm_distance_unavailable":
        return "OSRM نتوانست مسافت جاده‌ای کامل را برای همه مسیرها برگرداند."
    if reason == "visitor_start_missing":
        return "نقطه شروع یکی از ویزیتورها ثبت نشده است."
    if reason == "current_route_order_missing":
        return "ترتیب نهایی یکی از مسیرها کامل ثبت نشده است."
    if reason == "baseline_distance_zero":
        return "مسافت مسیر خام صفر است و درصد بهبود تعریف نمی‌شود."
    if reason == "assignments_missing":
        return "تخصیصی برای این تاریخ وجود ندارد."
    return "دلیل دقیق fallback مشخص نیست."


def _runtime_health_caption(runtime_state: dict[str, object]) -> str:
    osrm_text = "فعال" if bool(runtime_state.get("osrm_up", False)) else "غیرفعال"
    tiles_text = "فعال" if bool(runtime_state.get("tiles_up", False)) else "غیرفعال"
    osrm_latency = runtime_state.get("osrm_latency_ms")
    tiles_latency = runtime_state.get("tiles_latency_ms")
    osrm_latency_text = f"{osrm_latency}ms" if osrm_latency is not None else "—"
    tiles_latency_text = f"{tiles_latency}ms" if tiles_latency is not None else "—"
    return (
        f'پیش‌بررسی سرویس‌ها: <span class="ltr-inline">OSRM</span>={osrm_text} '
        f'(<span class="ltr-inline">{osrm_latency_text}</span>) | '
        f'<span class="ltr-inline">Tile</span>={tiles_text} '
        f'(<span class="ltr-inline">{tiles_latency_text}</span>)'
    )


def _render_offline_stack_starter(key_prefix: str) -> None:
    """Render the "Start offline services" recovery button when OSRM or
    Tile is down. The button shells out to ``docker compose up -d`` so the
    operator can recover without dropping to a terminal."""
    prereq = offline_stack_service.describe_prerequisites()
    if not prereq["docker_available"]:
        return
    if not prereq["osrm_graph_ready"]:
        st.caption(
            "گراف OSRM آماده نیست. ابتدا اسکریپت "
            "`scripts/offline_prepare_osrm_tehran.ps1` را اجرا کنید."
        )
        return

    if st.button(
        "راه‌اندازی خودکار سرویس‌های آفلاین",
        key=f"{key_prefix}_start_offline",
        help="docker compose up -d برای OSRM/VROOM/Tile",
    ):
        with st.spinner("در حال اجرای docker compose up -d ..."):
            result = offline_stack_service.bring_up_offline_stack()

        if not result["ok"]:
            st.error(result["message"])
            ps_output = offline_stack_service.docker_ps_summary()
            if ps_output:
                st.caption("وضعیت کانتینرها:")
                st.code(ps_output, language="text")
            return

        st.info(result["message"])
        if result.get("stderr"):
            st.caption("خروجی docker compose:")
            st.code(result["stderr"], language="text")

        with st.spinner("در انتظار سبز شدن health-check (حداکثر ۴۵ ثانیه)..."):
            health = offline_stack_service.wait_for_services(max_seconds=45)

        ps_output = offline_stack_service.docker_ps_summary()
        if health["osrm_up"] and health["tiles_up"]:
            st.success(
                f"OSRM و Tile در {health['elapsed_seconds']} ثانیه فعال شدند."
            )
            runtime_health_service.get_offline_runtime_status(force_refresh=True)
            st.rerun()
        else:
            messages = []
            if not health["osrm_up"]:
                messages.append("OSRM هنوز پاسخ نمی‌دهد (پورت 5000).")
            if not health["tiles_up"]:
                messages.append("Tile server هنوز پاسخ نمی‌دهد (پورت 8080).")
            st.warning(
                "کانتینرها اجرا شدند ولی همهٔ سرویس‌ها سبز نشدند: "
                + " | ".join(messages)
                + " می‌توانید چند ثانیهٔ دیگر صفحه را refresh کنید."
            )
            if ps_output:
                st.caption("وضعیت کانتینرها:")
                st.code(ps_output, language="text")


@st.cache_data(ttl=300, show_spinner=False)
def _cached_operational_snapshot(work_date_iso: str) -> tuple[dict, dict, list[dict]]:
    # FIX: [ARCH-03] Cache heavy read path and reduce repeated due-store recalculation cost.
    work_date = date.fromisoformat(work_date_iso)
    with get_db() as db:
        snapshot = assignment_service.get_work_date_operational_snapshot(db, work_date)
        kpis = reporting_export_service.get_daily_kpis(db, work_date)
        pending_queue = telesales_service.list_pending_followups(
            db, as_of_date=work_date
        )
    return snapshot, kpis, pending_queue


@st.cache_data(ttl=300, show_spinner=False)
def _cached_assignments(work_date_iso: str) -> pd.DataFrame:
    work_date = date.fromisoformat(work_date_iso)
    with get_db() as db:
        return dashboard_query_service.load_manager_assignments_df(
            db=db, work_date=work_date
        )


@st.cache_data(ttl=300, show_spinner=False)
def _cached_visitor_options(work_date_iso: str) -> dict[str, int]:
    work_date = date.fromisoformat(work_date_iso)
    with get_db() as db:
        return dashboard_query_service.get_visitor_options(db=db, work_date=work_date)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_route_map(work_date_iso: str, visitor_id: int) -> pd.DataFrame:
    work_date = date.fromisoformat(work_date_iso)
    with get_db() as db:
        return dashboard_query_service.load_route_map_df(
            db=db,
            work_date=work_date,
            visitor_id=visitor_id,
        )


def _run_apply_files_and_build_route(
    work_date: date,
    current_user: dict,
    stores_file,
    daily_file,
) -> dict:
    # FIX: [UX-06] Expose granular pipeline progress with checklist + progress bar.
    if daily_file is None:
        raise ValueError("برای ساخت مسیر، بارگذاری فایل وضعیت روزانه الزامی است.")

    stores_path = _save_uploaded_excel(stores_file)
    daily_path = _save_uploaded_excel(daily_file)

    try:
        with st.status("در حال اجرای پایپلاین ساخت مسیر...", expanded=True) as status:
            checklist_box = st.empty()
            progress_box = st.empty()
            progress_note_box = st.empty()
            total_steps = 5
            completed_steps = 0
            assigned_count = 0
            unassigned_count = 0
            progress_reason = (
                "این نوار پیشرفت مربوط به فرآیند تولید تخصیص و ساخت مسیر است."
            )
            step_order = [
                ("stores", "بارگذاری فایل فروشگاه‌ها"),
                ("daily", "ثبت وضعیت روزانه ویزیتورها"),
                ("assign", "تولید تخصیص‌ها"),
                ("route", "مرتب‌سازی مسیرها"),
                ("quality", "ارزیابی کیفیت مسیر"),
            ]
            step_state = {step_id: "pending" for step_id, _ in step_order}
            step_text = {step_id: label for step_id, label in step_order}

            def _render_progress() -> None:
                pct = min(100, int((completed_steps / total_steps) * 100))
                progress_box.markdown(
                    f"""
                    <div class="pipeline-progress-wrap">
                        <div class="pipeline-progress-label">درصد پیشرفت ساخت مسیر و تخصیص: {pct}٪</div>
                        <div class="pipeline-progress-track">
                            <div class="pipeline-progress-fill" style="width:{pct}%;"></div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                progress_note_box.markdown(
                    (
                        '<div class="pipeline-progress-note">'
                        f"نکته: از {assigned_count + unassigned_count} فروشگاه قابل ویزیت، "
                        f"{assigned_count} فروشگاه تخصیص یافته و {unassigned_count} فروشگاه تخصیص نیافته است."
                        "<br/>"
                        f"دلیل: {progress_reason}"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

            def _render_steps() -> None:
                status_map = {
                    "pending": "در انتظار",
                    "running": '<span class="hourglass-spin">⏳</span> در حال اجرا',
                    "done": "انجام شد",
                    "warn": "پایان با هشدار",
                }
                lines: list[str] = []
                for index, (step_id, _) in enumerate(step_order, start=1):
                    lines.append(
                        f"{index}. {step_text[step_id]} — {status_map.get(step_state.get(step_id, 'pending'), 'در انتظار')}"
                    )
                checklist_box.markdown(
                    f'<div class="pipeline-checklist-right">{"<br/>".join(lines)}</div>',
                    unsafe_allow_html=True,
                )

            def _set_step(
                step_id: str,
                state: str,
                message: str | None = None,
                mark_complete: bool = False,
            ) -> None:
                nonlocal completed_steps
                step_state[step_id] = state
                if message is not None:
                    step_text[step_id] = message
                _render_steps()
                if mark_complete:
                    completed_steps += 1
                _render_progress()

            _render_steps()
            _render_progress()

            with get_db() as db:
                stores_processed = 0
                if stores_path:
                    stores_processed = import_service.import_stores_from_excel(
                        stores_path, db
                    )
                    _set_step(
                        "stores",
                        "done",
                        f"فروشگاه‌ها بارگذاری شدند ({stores_processed} ردیف)",
                        mark_complete=True,
                    )
                else:
                    # Optional input still counts as finished step for progress continuity.
                    _set_step(
                        "stores",
                        "done",
                        "فایل فروشگاه جدیدی بارگذاری نشد.",
                        mark_complete=True,
                    )

                daily_processed = import_daily_visitor_statuses_from_excel(
                    daily_path, db
                )
                _set_step(
                    "daily",
                    "done",
                    f"وضعیت روزانه ویزیتورها ثبت شد ({daily_processed} نفر)",
                    mark_complete=True,
                )

                status_count_for_selected_date = (
                    dashboard_query_service.get_daily_status_row_count(
                        db=db,
                        work_date=work_date,
                    )
                )
                if status_count_for_selected_date <= 0:
                    raise ValueError(
                        "فایل وضعیت روزانه برای تاریخ انتخاب‌شده ردیفی ندارد. تاریخ کاری را با work_date فایل یکسان کنید."
                    )

                _set_step("assign", "running", "در حال تولید تخصیص‌ها...")
                draft_summary = assignment_service.generate_draft_assignments(
                    db=db,
                    work_date=work_date,
                    manager_user_id=current_user["id"],
                    replace_existing_draft=True,
                )
                _set_step(
                    "assign",
                    "done",
                    (
                        f"{draft_summary.get('created_assignments', 0)} تخصیص ساخته شد | "
                        f"{draft_summary.get('unassigned_due_stores', 0)} فروشگاه تخصیص نیافت"
                    ),
                    mark_complete=True,
                )
                assigned_count = int(draft_summary.get("created_assignments", 0))
                unassigned_count = int(draft_summary.get("unassigned_due_stores", 0))
                if unassigned_count > 0:
                    progress_reason = "مجموع ظرفیت روزانه ویزیتورهای فعال کمتر از تعداد فروشگاه‌های قابل ویزیت بوده است."
                else:
                    progress_reason = (
                        "همه فروشگاه‌های قابل ویزیت در ظرفیت روزانه پوشش داده شدند."
                    )
                _render_progress()

                # FIX: [PERF-05] Preflight local runtime health once and keep route step fail-fast.
                runtime_state = runtime_health_service.get_offline_runtime_status(
                    force_refresh=True
                )
                st.markdown(
                    f'<div class="panel-description" style="text-align:right !important;margin:.25rem 0;">{_runtime_health_caption(runtime_state)}</div>',
                    unsafe_allow_html=True,
                )
                if not bool(runtime_state.get("osrm_data_ready", False)):
                    st.warning(
                        "فایل داده OSRM پیدا نشد. مسیر مورد انتظار: offline/osrm/data/tehran-latest.osrm"
                    )
                if not bool(runtime_state.get("tiles_data_ready", False)):
                    st.info(
                        "فایل MBTiles پیدا نشد. مسیر مورد انتظار: offline/tiles/data/*.mbtiles"
                    )
                if (not bool(runtime_state.get("osrm_up", False))) or (
                    not bool(runtime_state.get("tiles_up", False))
                ):
                    _render_offline_stack_starter(
                        key_prefix=f"pipeline_starter_{work_date.isoformat()}"
                    )
                route_summary: dict
                routes_precomputed = bool(draft_summary.get("routes_precomputed"))
                if routes_precomputed:
                    route_summary = dict(draft_summary.get("route_summary") or {})
                    route_summary.setdefault(
                        "total_assignments",
                        int(draft_summary.get("created_assignments", 0)),
                    )
                    route_summary.setdefault("total_visitors", 0)
                    route_summary.setdefault("osrm_routed", 0)
                    route_summary.setdefault("nn_routed", 0)
                    route_summary.setdefault("vroom_routed", 0)
                    route_summary.setdefault("vroom_used", True)
                    route_summary.setdefault("osrm_used", False)
                    route_summary.setdefault(
                        "solver_mode",
                        str(draft_summary.get("solver_mode") or "vroom"),
                    )
                    route_summary.setdefault(
                        "fallback_stage",
                        draft_summary.get("fallback_stage"),
                    )
                    route_summary.setdefault(
                        "solver_reason",
                        draft_summary.get("solver_reason"),
                    )
                    route_summary.setdefault("fallback_reason", None)
                    solver_mode_label = str(route_summary.get("solver_mode") or "")
                    if solver_mode_label.startswith("territory"):
                        if "osrm" in solver_mode_label:
                            route_message = (
                                "مسیرها با پهنه‌بندی جغرافیایی + OSRM آفلاین ساخته شدند."
                            )
                        else:
                            route_message = (
                                "مسیرها با پهنه‌بندی جغرافیایی + الگوریتم پشتیبان ساخته شدند."
                            )
                    elif solver_mode_label == "vroom":
                        route_message = "مسیرها با VROOM+OSRM آفلاین ساخته شدند."
                    else:
                        route_message = "مسیرها بر اساس برنامه‌ریز پیش‌فرض ساخته شدند."
                    _set_step("route", "done", route_message, mark_complete=True)
                else:
                    if runtime_state.get("osrm_up", False):
                        route_planner = routing_service.OSRMRoutePlanner(
                            fallback_planner=routing_service.NearestNeighborRoutePlanner(),
                            runtime_status=runtime_state,
                        )
                        _set_step(
                            "route", "running", "در حال مرتب‌سازی مسیرها با OSRM محلی..."
                        )
                    else:
                        route_planner = routing_service.NearestNeighborRoutePlanner()
                        _set_step(
                            "route",
                            "running",
                            "OSRM محلی در دسترس نیست؛ مرتب‌سازی با الگوریتم پشتیبان انجام می‌شود...",
                        )

                    route_summary = routing_service.apply_routes_for_work_date(
                        db=db,
                        work_date=work_date,
                        planner=route_planner,
                    )
                    if draft_summary.get("fallback_stage"):
                        route_summary["fallback_stage"] = draft_summary.get(
                            "fallback_stage"
                        )
                    if draft_summary.get("solver_reason"):
                        route_summary["solver_reason"] = draft_summary.get(
                            "solver_reason"
                        )
                    route_summary["solver_mode"] = str(
                        route_summary.get("solver_mode")
                        or draft_summary.get("solver_mode")
                        or "legacy"
                    )

                    if route_summary["osrm_used"]:
                        _set_step(
                            "route",
                            "done",
                            "مسیرها با OSRM بهینه شدند.",
                            mark_complete=True,
                        )
                    else:
                        fallback_reason = (
                            route_summary.get("fallback_reason")
                            or route_summary.get("solver_reason")
                        )
                        _set_step(
                            "route",
                            "warn",
                            f"از الگوریتم پشتیبان استفاده شد. ({_fallback_reason_fa(fallback_reason)})",
                            mark_complete=True,
                        )

                quality = assignment_service.evaluate_route_quality(
                    db=db,
                    work_date=work_date,
                    route_summary=route_summary,
                )
                if quality["comparable"]:
                    _set_step(
                        "quality",
                        "done",
                        f"کیفیت جاده‌ای: بهبود {quality['improvement_pct']}٪ نسبت به مسیر خام",
                        mark_complete=True,
                    )
                else:
                    _set_step(
                        "quality",
                        "warn",
                        "کیفیت جاده‌ای قابل مقایسه نیست؛ مسیر ساخته‌شده همچنان معتبر است.",
                        mark_complete=True,
                    )
                status.update(label="پایپلاین با موفقیت انجام شد.", state="complete")

            return {
                "stores_processed": stores_processed,
                "daily_processed": daily_processed,
                "draft_summary": draft_summary,
                "route_summary": route_summary,
                "quality": quality,
                "osrm_used": bool(route_summary["osrm_used"]),
                "shadow": draft_summary.get("shadow"),
                "runtime_status": runtime_state,
            }
    finally:
        for path in [stores_path, daily_path]:
            if path and os.path.exists(path):
                os.remove(path)


def _render_pipeline_result(result: dict) -> None:
    draft_summary = result.get("draft_summary", {})
    quality = result.get(
        "quality",
        {
            "baseline_km": 0.0,
            "optimized_km": 0.0,
            "current_km": 0.0,
            "saved_km": 0.0,
            "improvement_pct": 0.0,
            "target_pct": 20.0,
            "comparable": False,
            "route_build_status": "failed",
            "quality_target_status": "not_comparable",
            "fallback_reason": None,
            "passes_gate": False,
        },
    )
    route_summary = result.get(
        "route_summary",
        {
            "total_assignments": int(result.get("routed_count", 0)),
            "osrm_routed": 0,
            "nn_routed": 0,
            "vroom_routed": 0,
            "vroom_used": False,
            "osrm_used": False,
            "solver_mode": "legacy",
            "fallback_stage": None,
            "solver_reason": None,
            "fallback_reason": None,
        },
    )
    solver_mode = str(
        route_summary.get("solver_mode") or draft_summary.get("solver_mode") or "legacy"
    ).strip()
    fallback_stage = route_summary.get("fallback_stage") or draft_summary.get(
        "fallback_stage"
    )
    solver_reason = route_summary.get("solver_reason") or draft_summary.get(
        "solver_reason"
    )

    st.success(
        "فایل‌ها اعمال شد و مسیرها ساخته شدند. "
        f"فروشگاه پردازش‌شده: {result['stores_processed']} | "
        f"وضعیت روزانه پردازش‌شده: {result['daily_processed']} | "
        f"تخصیص ساخته‌شده: {draft_summary.get('created_assignments', 0)} | "
        f"مرتب‌سازی مسیر: {route_summary.get('total_assignments', 0)}"
    )

    comparable = bool(quality.get("comparable", False))
    route_status_text = {
        "success": "موفق",
        "fallback": "ساخته‌شده با پشتیبان",
        "failed": "ناموفق",
    }.get(str(quality.get("route_build_status") or "failed"), "نامشخص")
    quality_status_text = {
        "achieved": "هدف محقق شد",
        "below_target": "زیر هدف آزمایشی",
        "not_comparable": "غیرقابل مقایسه",
    }.get(
        str(quality.get("quality_target_status") or "not_comparable"),
        "غیرقابل مقایسه",
    )

    render_metric_grid(
        [
            ('مسافت مسیر خام (<span class="ltr-inline">km</span>)', quality["baseline_km"]),
            ('مسافت مسیر نهایی جاده‌ای (<span class="ltr-inline">km</span>)', quality.get("optimized_km", quality["current_km"])),
            ('مسافت صرفه‌جویی‌شده (<span class="ltr-inline">km</span>)', quality.get("saved_km", 0.0)),
            ("بهبود نسبت به مسیر خام", f"{quality['improvement_pct']}%"),
            ("وضعیت ساخت مسیر", route_status_text),
            ("وضعیت هدف کیفیت", quality_status_text),
        ]
    )
    if solver_mode == "vroom":
        route_mode_text = (
            f'مسیر {route_summary.get("vroom_routed", 0)} ویزیتور با '
            '<span class="ltr-inline">VROOM+OSRM</span> آفلاین'
        )
    else:
        route_mode_text = (
            f'مسیر {route_summary.get("osrm_routed", 0)} ویزیتور با '
            '<span class="ltr-inline">OSRM</span> | '
            f"{route_summary.get('nn_routed', 0)} ویزیتور با الگوریتم پشتیبان"
        )
    st.markdown(
        (
            '<div style="direction:rtl;text-align:right;color:#6B7280;font-size:.85rem;margin-top:.1rem;">'
            f"{route_mode_text}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )
    if fallback_stage == "vroom_to_legacy":
        st.markdown(
            (
                '<div style="direction:rtl;text-align:right;color:#6B7280;font-size:.85rem;margin-top:.05rem;">'
                '<span class="ltr-inline">fallback</span> مرحله اول: '
                '<span class="ltr-inline">VROOM</span> به '
                f'<span class="ltr-inline">Legacy</span> ({_fallback_reason_fa(solver_reason)})'
                "</div>"
            ),
            unsafe_allow_html=True,
        )
    if int(route_summary.get("nn_routed", 0)) > 0:
        st.markdown(
            (
                '<div style="direction:rtl;text-align:right;color:#6B7280;font-size:.85rem;margin-top:.05rem;">'
                'دلیل <span class="ltr-inline">fallback</span>: '
                f"{_fallback_reason_fa(route_summary.get('fallback_reason'))}"
                "</div>"
            ),
            unsafe_allow_html=True,
        )
    if (
        int(route_summary.get("osrm_routed", 0)) == 0
        and int(route_summary.get("nn_routed", 0)) > 0
        and solver_mode != "vroom"
    ):
        runtime_state = result.get("runtime_status") or {}
        reason_text = _fallback_reason_fa(route_summary.get("fallback_reason"))
        if bool(runtime_state.get("osrm_up", False)):
            st.warning(
                f"OSRM پاسخ قابل استفاده نداد و مسیرها با الگوریتم پشتیبان ساخته شدند. {reason_text}"
            )
        else:
            st.warning(
                f"OSRM محلی در دسترس نبوده و مسیرها با الگوریتم پشتیبان ساخته شده‌اند. {reason_text}"
            )
    if fallback_stage == "vroom_to_legacy":
        st.info(
            "حل‌گر VROOM در دسترس نبود و سیستم به‌صورت خودکار از پایپلاین Legacy استفاده کرده است. "
            f"{_fallback_reason_fa(solver_reason)}"
        )
    shadow = result.get("shadow")
    if isinstance(shadow, dict) and bool(shadow.get("enabled")):
        shadow_status = "موفق" if str(shadow.get("status")) == "ok" else "ناموفق"
        shadow_reason = _fallback_reason_fa(shadow.get("solver_reason"))
        st.caption(
            "خروجی Shadow VROOM: "
            f"{shadow_status} | "
            f"assigned={shadow.get('assigned_count', 0)} | "
            f"unassigned={shadow.get('unassigned_count', 0)} | "
            f"reason={shadow_reason}"
        )
    if comparable and not quality["passes_gate"]:
        st.info(
            "مسیرها با OSRM با موفقیت ساخته شدند. "
            f"بهبود جاده‌ای {quality['improvement_pct']}٪ است و به هدف آزمایشی "
            f"{quality.get('target_pct', 20.0):g}٪ نرسیده است."
        )
    if not comparable:
        st.info(
            "محاسبه کیفیت جاده‌ای کامل نشد؛ این وضعیت مستقل از موفقیت ساخت مسیر است. "
            f"دلیل: {_fallback_reason_fa(quality.get('comparison_error'))}"
        )


def render_manager_dashboard(current_user: dict) -> None:
    work_date = jalali_date_input(
        label="📅 تاریخ کاری",
        key_prefix="manager_work_date",
        default_gregorian=date.today(),
    )
    work_date_iso = work_date.isoformat()

    # FIX: [BIZ-02] Avoid stale pipeline result after work-date change.
    stored_date = st.session_state.get("manager_last_pipeline_date")
    if stored_date and stored_date != work_date_iso:
        st.session_state.pop("manager_last_pipeline_result", None)
        st.session_state.pop("manager_last_pipeline_date", None)

    snapshot, kpis, pending_queue = _cached_operational_snapshot(work_date_iso)
    visit_count = int(snapshot.get("visit_count", 0))
    followup_count = int(snapshot.get("followup_count", 0))
    published_count = int(snapshot.get("published_count", 0))
    draft_count = int(snapshot.get("draft_count", 0))
    non_draft_count = int(snapshot.get("non_draft_count", 0))
    assignment_count = int(snapshot.get("assignment_count", 0))

    # FIX: Allow upload/flush when only publish lock exists and no operational records are created yet.
    is_hard_locked = visit_count > 0 or followup_count > 0
    is_soft_locked = (not is_hard_locked) and non_draft_count > 0

    if is_hard_locked or is_soft_locked:
        st.warning(
            "این روز بسته شده است — "
            f"{visit_count} ویزیت، {followup_count} پیگیری، "
            f"{published_count} تخصیص منتشرشده. برای ساخت مسیر جدید، تاریخ دیگری "
            "انتخاب کنید یا از پاک‌سازی کامل استفاده کنید."
        )
    if is_soft_locked:
        st.info(
            "برای این تاریخ هنوز ویزیت/پیگیری ثبت نشده است. می‌توانید پاک‌سازی کامل انجام دهید و دوباره فایل آپلود کنید."
        )
    if (not is_hard_locked) and draft_count <= 0:
        st.info(
            "برای این تاریخ هنوز مسیری ساخته نشده است. فایل وضعیت روزانه را "
            "بارگذاری کنید. (اگر قبلاً منتشر شده، ساخت مجدد فقط پس از پاک‌سازی کامل "
            "همین تاریخ ممکن است.)"
        )

    with st.expander("اعمال فایل‌ها و ساخت مسیر", expanded=not is_hard_locked):
        st.markdown(
            """
            <div class="panel-description">
                با آپلود فایل فروشگاه‌ها، اطلاعات فروشگاه‌ها به‌روزرسانی می‌شود.<br/>
                با آپلود فایل وضعیت روزانه ویزیتورها، ظرفیت و نقطه شروع هر ویزیتور ثبت می‌شود و مسیرها برای همان تاریخ دوباره طراحی می‌شوند.
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="panel-description-columns">
                ستون‌های الزامی فایل وضعیت روزانه:
                <span class="ltr-inline">work_date, username, visitor_code, full_name, start_lat, start_lon, capacity, is_active_today</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.container(key="manager_upload_stack"):
            with st.container(key="manager_stores_upload_field"):
                stores_file = st.file_uploader(
                    "فایل فروشگاه‌ها (اختیاری)",
                    type=["xlsx"],
                    key=f"stores_apply_{work_date_iso}",
                    disabled=is_hard_locked,
                )
                st.caption("یک فایل XLSX؛ برای به‌روزرسانی اطلاعات فروشگاه‌ها اختیاری است.")

            with st.container(key="manager_daily_upload_field"):
                daily_file = st.file_uploader(
                    "فایل وضعیت روزانه ویزیتورها (اجباری)",
                    type=["xlsx"],
                    key=f"daily_apply_{work_date_iso}",
                    disabled=is_hard_locked,
                )
                st.caption("یک فایل XLSX؛ برای ثبت ظرفیت و نقطه شروع ویزیتورها الزامی است.")

        if st.button(
            "اعمال فایل‌ها و ساخت مسیر",
            key=f"build_pipeline_{work_date_iso}",
            disabled=is_hard_locked,
            type="primary",
        ):
            if is_soft_locked:
                st.error(
                    "برای این تاریخ تخصیص منتشرشده وجود دارد. ابتدا «پاک‌سازی کامل همین تاریخ» را انجام دهید، سپس مسیر جدید بسازید."
                )
                st.stop()
            try:
                result = _run_apply_files_and_build_route(
                    work_date=work_date,
                    current_user=current_user,
                    stores_file=stores_file,
                    daily_file=daily_file,
                )
                st.session_state["manager_last_pipeline_result"] = result
                st.session_state["manager_last_pipeline_date"] = work_date_iso
                st.cache_data.clear()
                st.rerun()
            except DomainError as exc:
                # Business-rule rejection (e.g. routing for this date already
                # past the draft stage). The message is already operator-facing
                # and tells the user exactly which recovery action to take.
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"خطا در ساخت مسیر: {exc}")

        last_result = st.session_state.get("manager_last_pipeline_result")
        if isinstance(last_result, dict):
            _render_pipeline_result(last_result)

    with st.expander("پاک‌سازی کامل داده‌های همین تاریخ", expanded=False):
        has_operational_data = visit_count > 0 or followup_count > 0
        has_any_daily_data = assignment_count > 0 or has_operational_data
        st.markdown(
            """
            <div class="panel-description">
                این عملیات همه داده‌های تخصیص، ویزیت و پیگیری فروش تلفنی مربوط به همین تاریخ را حذف می‌کند
                و باعث از دست رفتن سوابق عملیاتی همان روز می‌شود.
            </div>
            """,
            unsafe_allow_html=True,
        )
        if not has_any_daily_data:
            st.info("برای این تاریخ داده عملیاتی یا تخصیصی ثبت نشده است.")
        elif not has_operational_data and has_any_daily_data:
            st.info(
                "برای این تاریخ هنوز ویزیت یا پیگیری عملیاتی ثبت نشده است؛ پاک‌سازی کامل بدون از دست‌رفتن سابقه ویزیت انجام می‌شود."
            )
        confirm_flush = st.checkbox(
            "تأیید می‌کنم پاکسازی کامل فقط برای همین تاریخ انجام شود.",
            key=f"confirm_flush_{work_date_iso}",
        )
        if st.button(
            "پاک‌سازی کامل همین تاریخ",
            key=f"flush_{work_date_iso}",
            disabled=(not confirm_flush) or (not has_any_daily_data),
        ):
            try:
                with get_db() as db:
                    result = assignment_service.flush_work_date_operational_data(
                        db=db,
                        work_date=work_date,
                        manager_user_id=current_user["id"],
                    )
                st.success(
                    f"پاک‌سازی انجام شد: {result['assignments_deleted']} تخصیص، "
                    f"{result['visits_deleted']} نتیجهٔ ویزیت و "
                    f"{result['followups_deleted']} پیگیری حذف شدند."
                )
                st.session_state.pop("manager_last_pipeline_result", None)
                st.session_state.pop("manager_last_pipeline_date", None)
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"خطا در پاک‌سازی: {exc}")

    neu_section_header("عملیات مدیریتی")
    op1, op2 = st.columns([1, 7], gap="small")

    with op1:
        if st.button(
            "انتشار مسیرها",
            key=f"publish_{work_date_iso}",
            disabled=is_hard_locked or (draft_count <= 0),
            type="primary",
        ):
            try:
                with get_db() as db:
                    count = assignment_service.publish_assignments(
                        db=db,
                        work_date=work_date,
                        manager_user_id=current_user["id"],
                        include_draft_override=True,
                    )
                st.success(f"{count} تخصیص منتشر شد.")
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"خطا در انتشار مسیرها: {exc}")

    with op2:
        # FIX: Two-step confirmation. Auto-marking unsubmitted assignments as
        # red is destructive (creates visits, pushes stores into telesales
        # queue) so we preview the count first and confirm against it.
        preview_key = f"finalize_preview_{work_date_iso}"
        confirm_key = f"finalize_confirm_{work_date_iso}"
        if st.button(
            "نهایی‌سازی موارد ثبت‌نشده",
            key=f"finalize_{work_date_iso}",
            disabled=is_hard_locked,
        ):
            with get_db() as db:
                pending_preview = visit_service.list_unsubmitted_assignments_preview(
                    db=db, work_date=work_date
                )
            st.session_state[preview_key] = pending_preview
            st.session_state[confirm_key] = False

        pending_preview = st.session_state.get(preview_key)
        if pending_preview is not None:
            pending_count = len(pending_preview)
            if pending_count == 0:
                st.info("هیچ تخصیص ثبت‌نشده‌ای وجود ندارد.")
                st.session_state.pop(preview_key, None)
            else:
                st.warning(
                    f"{pending_count} تخصیص ثبت‌نشده به‌صورت خودکار قرمز و وارد صف "
                    "فروش تلفنی خواهند شد. این عمل بازگشت‌پذیر نیست. آیا مطمئن‌اید؟"
                )
                confirmed = st.checkbox(
                    "تأیید می‌کنم",
                    key=confirm_key,
                )
                if st.button(
                    "اجرای نهایی‌سازی",
                    key=f"finalize_execute_{work_date_iso}",
                    disabled=not confirmed,
                ):
                    try:
                        with get_db() as db:
                            count = visit_service.finalize_unsubmitted_assignments(
                                db=db,
                                work_date=work_date,
                                actor_user_id=current_user["id"],
                                expected_pending_count=pending_count,
                            )
                        st.success(
                            f"{count} تخصیص ثبت‌نشده به‌صورت خودکار قرمز شد."
                        )
                        st.session_state.pop(preview_key, None)
                        st.session_state.pop(confirm_key, None)
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as exc:
                        st.error(f"خطا در نهایی‌سازی: {exc}")

    neu_section_header("شاخص‌های روزانه")
    render_metric_grid(
        [
            ("فروشگاه‌های موعددار امروز", kpis["due_stores"]),
            ("سبز / زرد / قرمز", f"{kpis['green']} / {kpis['yellow']} / {kpis['red']}"),
            ("ویزیت تکمیل‌شده", kpis["completed_visits"]),
            ("تخصیص‌شده", kpis["assigned_stores"]),
            ("صف تماس تلفنی (کل صف فعال)", kpis["telesales_queue_size"]),
        ]
    )
    if int(kpis.get("due_stores", 0)) == 0:
        st.info(get_empty_state_message(role="manager", context="no_due_stores"))

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    neu_section_header("جدول تخصیص‌ها")
    assignment_df = _cached_assignments(work_date_iso)
    if assignment_df.empty:
        st.info(get_empty_state_message(role="manager", context="no_assignments"))
    else:
        assignment_view_df = assignment_df.copy()
        # FIX: Show store grade in manager-generated assignment list for route review.
        if "store_grade" in assignment_view_df.columns:
            assignment_view_df["store_grade"] = assignment_view_df[
                "store_grade"
            ].fillna("نامشخص")
        if "assignment_id" in assignment_view_df.columns:
            assignment_view_df["assignment_id"] = range(1, len(assignment_view_df) + 1)
        render_rtl_table(
            assignment_view_df,
            key_prefix=f"manager_assignments_{work_date_iso}",
        )

    neu_section_header("صف در انتظار تماس تلفنی")
    if pending_queue:
        pending_df = pd.DataFrame(pending_queue)
        pending_df = pending_df.drop(
            columns=["store_lat", "store_lon"], errors="ignore"
        )
        render_rtl_table(
            pending_df,
            key_prefix=f"manager_pending_telesales_{work_date_iso}",
        )
    else:
        st.info(get_empty_state_message(role="manager", context="no_pending_telesales"))

    visitor_options = _cached_visitor_options(work_date_iso)
    neu_section_header("نقشه مسیر")
    runtime_state = runtime_health_service.get_offline_runtime_status()
    services_down = (not bool(runtime_state.get("osrm_up", False))) or (
        not bool(runtime_state.get("tiles_up", False))
    )
    if services_down:
        st.info(
            "حالت کاهشی فعال است: اگر OSRM یا Tile در دسترس نباشد، نقشه مینیمال سریع نمایش داده می‌شود."
        )
        _render_offline_stack_starter(key_prefix=f"map_starter_{work_date_iso}")
    if not bool(runtime_state.get("osrm_data_ready", False)):
        st.warning("فایل داده OSRM پیدا نشد (offline/osrm/data/tehran-latest.osrm).")
    if not bool(runtime_state.get("tiles_data_ready", False)):
        st.warning("فایل MBTiles پیدا نشد (offline/tiles/data/*.mbtiles).")
    st.markdown(
        '<div class="panel-description" style="text-align:right !important;margin:0.1rem 0 0.35rem;">'
        'راهنما: در همین کادر انتخاب ویزیتور می‌توانید جست‌وجو کنید (نمونه: <span class="ltr-inline">VIS-001</span> یا <span class="ltr-inline">visitor1</span>).'
        "</div>",
        unsafe_allow_html=True,
    )
    selected_code = st.selectbox(
        "انتخاب ویزیتور برای نمایش نقشه و دانلود مسیر",
        options=list(visitor_options.keys()),
        index=None,
        placeholder="ویزیتور را انتخاب یا جست‌وجو کنید...",
        key=f"manager_map_visitor_{work_date_iso}",
    )

    if selected_code:
        selected_id = visitor_options[selected_code]
        route_df = _cached_route_map(work_date_iso, selected_id)
        if route_df.empty:
            st.info(
                get_empty_state_message(role="manager", context="no_route_for_visitor")
            )
        else:
            # FIX: [PERF-04] Pass one pre-read runtime state to map component and avoid duplicate probes/messages.
            render_route_map(
                route_df,
                runtime_status=runtime_state,
                show_runtime_messages=False,
            )

    neu_section_header("خروجی‌ها")
    ex1, ex2 = st.columns([1, 1], gap="small")
    with ex1:
        if selected_code:
            with get_db() as db:
                route_buf = reporting_export_service.export_visitor_route_excel(
                    db=db,
                    work_date=work_date,
                    visitor_id=visitor_options[selected_code],
                )
            st.download_button(
                label=f"دانلود مسیر {selected_code}",
                data=route_buf.getvalue(),
                file_name=f"route_{work_date_iso}_{selected_code}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    with ex2:
        with get_db() as db:
            summary_buf = reporting_export_service.export_manager_daily_summary_excel(
                db=db,
                work_date=work_date,
            )
        st.download_button(
            label="دانلود گزارش کامل روزانه",
            data=summary_buf.getvalue(),
            file_name=f"summary_{work_date_iso}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
