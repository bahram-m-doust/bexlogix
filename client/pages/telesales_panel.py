# Purpose: Telesales follow-up queue and result submission UI.
# Workflow Role: Handles red-visit follow-ups with paged workflow for long queues.

from __future__ import annotations

from datetime import date
import html
import re

import pandas as pd
import streamlit as st

from client.components.empty_state import get_empty_state_message
from client.components.jalali_date import jalali_date_input
from client.components.rtl_table import render_rtl_table
from client.styles.neumorphism import neu_metric, neu_section_header, render_page_title
from server.app.enums.contact_status import ContactStatus
from server.app.enums.telesales_outcome import TelesalesOutcome
from server.app.services import telesales_service
from server.db.database import get_db

CONTACT_LABELS = {
    ContactStatus.REACHED.value: "تماس برقرار شد",
    ContactStatus.NOT_REACHED.value: "عدم برقراری تماس",
}

OUTCOME_LABELS = {
    TelesalesOutcome.SALE_DONE.value: "فروش انجام شد",
    TelesalesOutcome.NO_NEED.value: "نیازی نبود",
    TelesalesOutcome.POSTPONE.value: "موکول شد (پیگیری جدید برای فردا ساخته می‌شود)",
    TelesalesOutcome.INVALID.value: "نامعتبر",
}

ITEMS_PER_PAGE = 10
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_EMPTY_TEXT_MARKERS = {"", "nan", "none", "<div/>", "<div></div>", "<p></p>", "<br>", "<br/>", "<br />"}


def _safe_text(value: str | None) -> str:
    # FIX: Sanitize rich-text placeholders so raw HTML fragments never leak into UI.
    raw_text = str(value or "").strip()
    if raw_text.lower() in _EMPTY_TEXT_MARKERS:
        return "—"
    without_tags = _HTML_TAG_RE.sub(" ", raw_text)
    text = html.unescape(without_tags).replace("\xa0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if text.lower() in _EMPTY_TEXT_MARKERS or not text:
        return "—"
    return text


def _get_paginated_slice(items: list[dict], page: int) -> tuple[list[dict], int, int, int]:
    total = len(items)
    total_pages = max((total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE, 1)
    safe_page = min(max(page, 1), total_pages)
    start_idx = (safe_page - 1) * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, total)
    return items[start_idx:end_idx], safe_page, start_idx + 1 if total > 0 else 0, end_idx

def render_telesales_panel(current_user: dict) -> None:
    as_of_date = jalali_date_input(
        label="📅 نمایش موارد تا تاریخ",
        key_prefix="telesales_as_of_date",
        default_gregorian=date.today(),
    )

    with get_db() as db:
        pending_followups = telesales_service.list_pending_followups(
            db=db,
            as_of_date=as_of_date,
        )

    c1, _ = st.columns([1, 3])
    with c1:
        neu_metric("آیتم‌های در انتظار", len(pending_followups))

    if not pending_followups:
        st.success(get_empty_state_message(role="telesales", context="no_queue"))
        return

    if "telesales_page" not in st.session_state:
        st.session_state["telesales_page"] = 1

    page_items, page, from_item, to_item = _get_paginated_slice(
        pending_followups,
        int(st.session_state.get("telesales_page", 1)),
    )
    st.session_state["telesales_page"] = page
    total_items = len(pending_followups)
    total_pages = max((total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE, 1)

    nav_left, nav_center, nav_right = st.columns([1, 2, 1], gap="small")
    with nav_left:
        if st.button("قبلی", key="telesales_prev_page", disabled=page <= 1):
            st.session_state["telesales_page"] = max(page - 1, 1)
            st.rerun()
    with nav_center:
        st.markdown(
            f'<div class="panel-description" style="text-align:center !important;">آیتم {from_item} تا {to_item} از {total_items} | صفحه {page} از {total_pages}</div>',
            unsafe_allow_html=True,
        )
    with nav_right:
        if st.button(
            "بعدی",
            key="telesales_next_page",
            disabled=page >= total_pages,
        ):
            st.session_state["telesales_page"] = min(page + 1, total_pages)
            st.rerun()

    st.markdown('<div class="section-gap-lg"></div>', unsafe_allow_html=True)
    neu_section_header("صف پیگیری در انتظار")
    queue_df = pd.DataFrame(
        [
            {
                "پیگیری": item["followup_id"],
                "تاریخ پیگیری": item["followup_date"],
                "فروشگاه": f"{item['store_code']} — {item['store_name']}",
                "ویزیتور": _safe_text(item.get("visitor_code")),
                "علت عدم انجام ویزیت": _safe_text(item.get("unavailable_reason")),
            }
            for item in page_items
        ]
    )
    render_rtl_table(
        queue_df,
        key_prefix=f"telesales_queue_{as_of_date.isoformat()}_{page}",
        max_height_px=340,
    )

    neu_section_header("ثبت نتیجه پیگیری")
    for item in page_items:
        followup_id = item["followup_id"]
        title = f"{item['store_code']} — {item['store_name']} | پیگیری #{followup_id}"

        with st.expander(title):
            st.markdown('<div class="neu-card-flat">', unsafe_allow_html=True)
            st.markdown(
                f'<div class="telesales-detail-line"><strong>تاریخ پیگیری:</strong> <span class="ltr-inline">{_safe_text(item.get("followup_date"))}</span></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>تاریخ ویزیت قرمز:</strong> <span class="ltr-inline">{_safe_text(item.get("visit_date"))}</span></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>ویزیتور:</strong> <span class="ltr-inline">{_safe_text(item.get("visitor_code"))}</span></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>منطقه فروشگاه:</strong> {_safe_text(item.get("store_region"))}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>آدرس فروشگاه:</strong> {_safe_text(item.get("store_address"))}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>مختصات:</strong> <span class="ltr-inline">{_safe_text(item.get("store_lat"))}, {_safe_text(item.get("store_lon"))}</span></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="telesales-detail-line"><strong>علت عدم انجام ویزیت:</strong> {_safe_text(item.get("unavailable_reason"))}</div>',
                unsafe_allow_html=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)

            with st.form(f"followup_{followup_id}"):
                selected_contact = st.selectbox(
                    "وضعیت تماس",
                    options=list(CONTACT_LABELS.values()),
                    key=f"contact_{followup_id}",
                )
                contact_status = next(
                    code for code, label in CONTACT_LABELS.items() if label == selected_contact
                )

                selected_outcome = st.selectbox(
                    "نتیجه پیگیری",
                    options=list(OUTCOME_LABELS.values()),
                    key=f"result_{followup_id}",
                )
                outcome = next(
                    code for code, label in OUTCOME_LABELS.items() if label == selected_outcome
                )
                note = st.text_area(
                    "یادداشت اپراتور",
                    key=f"note_{followup_id}",
                    placeholder="یادداشت اختیاری پیگیری...",
                )
                submitted = st.form_submit_button(
                    "ذخیره نتیجه",
                    type="primary",
                )

            if submitted:
                try:
                    with get_db() as db:
                        telesales_service.submit_followup_result(
                            db=db,
                            followup_id=followup_id,
                            telesales_user_id=current_user["id"],
                            contact_status=contact_status,
                            result=outcome,
                            note=note,
                        )
                    st.success("نتیجه پیگیری با موفقیت ذخیره شد.")
                    # FIX: [UX-02] Keep current page after submit to prevent scroll loss.
                    st.session_state["telesales_page"] = page
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"خطا در ذخیره نتیجه: {exc}")
