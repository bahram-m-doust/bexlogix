# Purpose: Shared Jalali date picker for all dashboards.
# Workflow Role: Provides a consistent Persian date selection UX with optional today shortcut.

from __future__ import annotations

from datetime import date as gregorian_date

import jdatetime
import streamlit as st

_PERSIAN_MONTHS = {
    1: "فروردین",
    2: "اردیبهشت",
    3: "خرداد",
    4: "تیر",
    5: "مرداد",
    6: "شهریور",
    7: "مهر",
    8: "آبان",
    9: "آذر",
    10: "دی",
    11: "بهمن",
    12: "اسفند",
}

_EN_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA_TO_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_WEEKDAY_LABELS = (
    ("ش", "شنبه"),
    ("ی", "یکشنبه"),
    ("د", "دوشنبه"),
    ("س", "سه‌شنبه"),
    ("چ", "چهارشنبه"),
    ("پ", "پنجشنبه"),
    ("ج", "جمعه"),
)


# Contract: _to_fa_digits executes one deterministic step in the workflow.
def _to_fa_digits(value: str | int) -> str:
    return str(value).translate(_EN_TO_FA_DIGITS)


# Contract: _to_en_digits executes one deterministic step in the workflow.
def _to_en_digits(value: str) -> str:
    return str(value).translate(_FA_TO_EN_DIGITS)


# Contract: _jalali_days_in_month executes one deterministic step in the workflow.
def _jalali_days_in_month(year: int, month: int) -> int:
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    return 30 if jdatetime.date(year, 1, 1).isleap() else 29


def _month_label(month: int) -> str:
    return f"{_to_fa_digits(f'{month:02d}')} - {_PERSIAN_MONTHS[month]}"


def _month_number(value: str, fallback: int) -> int:
    try:
        parsed = int(_to_en_digits(str(value).split("-")[0].strip()))
    except (TypeError, ValueError):
        return fallback
    return max(1, min(12, parsed))


def _adjacent_month(year: int, month: int, delta: int) -> tuple[int, int]:
    zero_based = (year * 12) + (month - 1) + delta
    target_year, target_month = divmod(zero_based, 12)
    return target_year, target_month + 1


def _calendar_cells(year: int, month: int) -> list[tuple[int, int, int, bool]]:
    first_gregorian = jdatetime.date(year, month, 1).togregorian()
    first_day_offset = (first_gregorian.weekday() + 2) % 7
    previous_year, previous_month = _adjacent_month(year, month, -1)
    next_year, next_month = _adjacent_month(year, month, 1)
    previous_month_days = _jalali_days_in_month(previous_year, previous_month)
    current_month_days = _jalali_days_in_month(year, month)

    cells: list[tuple[int, int, int, bool]] = []
    for day in range(previous_month_days - first_day_offset + 1, previous_month_days + 1):
        cells.append((previous_year, previous_month, day, True))
    for day in range(1, current_month_days + 1):
        cells.append((year, month, day, False))

    row_count = max(5, (len(cells) + 6) // 7)
    trailing_days = (row_count * 7) - len(cells)
    for day in range(1, trailing_days + 1):
        cells.append((next_year, next_month, day, True))
    return cells


def _set_jalali_session_date(
    year_key: str,
    month_key: str,
    day_key: str,
    year: int,
    month: int,
    day: int,
) -> None:
    st.session_state[year_key] = int(year)
    st.session_state[month_key] = _month_label(int(month))
    st.session_state[day_key] = int(day)


def _shift_jalali_session_month(
    year_key: str,
    month_key: str,
    day_key: str,
    delta: int,
    minimum_year: int,
    maximum_year: int,
) -> None:
    current_year = int(st.session_state[year_key])
    current_month = _month_number(str(st.session_state[month_key]), 1)
    current_index = (current_year * 12) + (current_month - 1)
    minimum_index = minimum_year * 12
    maximum_index = (maximum_year * 12) + 11
    target_index = max(minimum_index, min(maximum_index, current_index + delta))
    target_year, zero_based_month = divmod(target_index, 12)
    target_month = zero_based_month + 1
    target_day = min(
        int(st.session_state.get(day_key, 1)),
        _jalali_days_in_month(target_year, target_month),
    )
    _set_jalali_session_date(
        year_key,
        month_key,
        day_key,
        target_year,
        target_month,
        target_day,
    )


def _render_jalali_calendar(
    *,
    key_prefix: str,
    safe_prefix: str,
    year_key: str,
    month_key: str,
    day_key: str,
    today_jalali: jdatetime.date,
    year_options: list[int],
) -> None:
    minimum_year, maximum_year = year_options[0], year_options[-1]
    month_labels = [_month_label(idx) for idx in range(1, 13)]
    view_year_key = f"{key_prefix}_calendar_view_year"
    view_month_key = f"{key_prefix}_calendar_view_month"
    view_day_key = f"{key_prefix}_calendar_view_day"

    with st.container(key=f"bex_jalali_calendar_{safe_prefix}"):
        with st.container(key=f"bex_jalali_header_{safe_prefix}"):
            previous_col, controls_col, next_col = st.columns(
                [1, 5, 1],
                gap=None,
                vertical_alignment="center",
            )
            with previous_col:
                st.button(
                    "ماه قبل",
                    key=f"bex_jalali_prev_{safe_prefix}",
                    disabled=(
                        int(st.session_state[view_year_key]) == minimum_year
                        and _month_number(str(st.session_state[view_month_key]), 1) == 1
                    ),
                    on_click=_shift_jalali_session_month,
                    args=(
                        view_year_key,
                        view_month_key,
                        view_day_key,
                        -1,
                        minimum_year,
                        maximum_year,
                    ),
                )
            with controls_col:
                month_col, year_col = st.columns([1.45, 1], gap="small")
                with month_col:
                    selected_month_label = st.selectbox(
                        "ماه",
                        options=month_labels,
                        key=view_month_key,
                        format_func=lambda value: str(value).split("-", 1)[-1].strip(),
                        label_visibility="collapsed",
                    )
                with year_col:
                    year = st.selectbox(
                        "سال",
                        options=year_options,
                        key=view_year_key,
                        format_func=lambda value: _to_fa_digits(value),
                        label_visibility="collapsed",
                    )
            with next_col:
                st.button(
                    "ماه بعد",
                    key=f"bex_jalali_next_{safe_prefix}",
                    disabled=(
                        int(st.session_state[view_year_key]) == maximum_year
                        and _month_number(str(st.session_state[view_month_key]), 12) == 12
                    ),
                    on_click=_shift_jalali_session_month,
                    args=(
                        view_year_key,
                        view_month_key,
                        view_day_key,
                        1,
                        minimum_year,
                        maximum_year,
                    ),
                )

        calendar_year = int(year)
        calendar_month = _month_number(selected_month_label, int(today_jalali.month))
        committed_year = int(st.session_state[year_key])
        committed_month = _month_number(
            str(st.session_state[month_key]),
            int(today_jalali.month),
        )
        committed_day = int(st.session_state[day_key])

        with st.container(key=f"bex_jalali_month_{safe_prefix}"):
            weekday_html = "".join(
                f'<abbr title="{full_label}">{short_label}</abbr>'
                for short_label, full_label in _WEEKDAY_LABELS
            )
            st.markdown(
                f'<div class="bex-jalali-weekdays">{weekday_html}</div>',
                unsafe_allow_html=True,
            )

            with st.container(key=f"bex_jalali_grid_{safe_prefix}"):
                cells = _calendar_cells(calendar_year, calendar_month)
                for week_start in range(0, len(cells), 7):
                    week_columns = st.columns(7, gap=None)
                    for column, (cell_year, cell_month, cell_day, is_outside) in zip(
                        week_columns,
                        cells[week_start : week_start + 7],
                    ):
                        is_selected = (
                            cell_year == committed_year
                            and cell_month == committed_month
                            and cell_day == committed_day
                        )
                        is_today = (
                            cell_year == int(today_jalali.year)
                            and cell_month == int(today_jalali.month)
                            and cell_day == int(today_jalali.day)
                        )
                        state_name = (
                            "selected"
                            if is_selected
                            else "current"
                            if is_today
                            else "outside"
                            if is_outside
                            else "default"
                        )
                        cell_is_available = minimum_year <= cell_year <= maximum_year
                        with column:
                            day_selected = st.button(
                                _to_fa_digits(cell_day),
                                key=(
                                    f"bex_jalali_day_{state_name}_{safe_prefix}_"
                                    f"{cell_year}_{cell_month}_{cell_day}"
                                ),
                                type="primary" if is_selected else "tertiary",
                                disabled=not cell_is_available,
                            )
                            if day_selected:
                                _set_jalali_session_date(
                                    year_key,
                                    month_key,
                                    day_key,
                                    cell_year,
                                    cell_month,
                                    cell_day,
                                )
                                st.rerun(scope="app")

        with st.container(key=f"bex_jalali_dialog_actions_{safe_prefix}"):
            select_today = st.button(
                "انتخاب امروز",
                key=f"bex_jalali_select_today_{safe_prefix}",
            )
            if select_today:
                _set_jalali_session_date(
                    year_key,
                    month_key,
                    day_key,
                    int(today_jalali.year),
                    int(today_jalali.month),
                    int(today_jalali.day),
                )
                st.rerun(scope="app")


# Contract: jalali_date_input executes one deterministic step in the workflow.
def jalali_date_input(
    label: str,
    key_prefix: str,
    default_gregorian: gregorian_date | None = None,
) -> gregorian_date:
    default_base = default_gregorian or gregorian_date.today()
    default_jalali = jdatetime.date.fromgregorian(date=default_base)

    year_key = f"{key_prefix}_jalali_year"
    month_key = f"{key_prefix}_jalali_month"
    day_key = f"{key_prefix}_jalali_day"
    today_jalali = jdatetime.date.today()

    if year_key not in st.session_state:
        st.session_state[year_key] = int(default_jalali.year)
    if month_key not in st.session_state:
        st.session_state[month_key] = _month_label(int(default_jalali.month))
    if day_key not in st.session_state:
        st.session_state[day_key] = int(default_jalali.day)

    preview_year = int(st.session_state.get(year_key, int(default_jalali.year)))
    preview_month_label = str(st.session_state.get(month_key, ""))
    preview_month = _month_number(preview_month_label, int(default_jalali.month))
    preview_day = int(st.session_state.get(day_key, int(default_jalali.day)))
    preview_day = max(1, min(_jalali_days_in_month(preview_year, preview_month), preview_day))
    st.session_state[day_key] = preview_day

    preview_jalali = jdatetime.date(preview_year, preview_month, preview_day)
    preview_gregorian = preview_jalali.togregorian()
    preview_fa = f"{_to_fa_digits(preview_day)} {_PERSIAN_MONTHS[preview_month]} {_to_fa_digits(preview_year)}"

    year_options = list(range(default_jalali.year - 5, default_jalali.year + 6))
    safe_prefix = key_prefix.replace("-", "_")
    view_year_key = f"{key_prefix}_calendar_view_year"
    view_month_key = f"{key_prefix}_calendar_view_month"
    view_day_key = f"{key_prefix}_calendar_view_day"

    @st.dialog("انتخاب تاریخ کاری", width="small")
    def _calendar_dialog() -> None:
        _render_jalali_calendar(
            key_prefix=key_prefix,
            safe_prefix=safe_prefix,
            year_key=year_key,
            month_key=month_key,
            day_key=day_key,
            today_jalali=today_jalali,
            year_options=year_options,
        )

    caption_col, today_col = st.columns([5, 1], gap="small", vertical_alignment="center")
    with caption_col:
        st.markdown(
            f'<div class="jalali-selected-caption">تاریخ انتخابی: {preview_fa} '
            f'(معادل میلادی: <span class="ltr-inline">{preview_gregorian.isoformat()}</span>)</div>',
            unsafe_allow_html=True,
        )
    with today_col:
        open_calendar = st.button(
            "امروز",
            key=f"{key_prefix}_today_shortcut",
            use_container_width=False,
        )
    if open_calendar:
        st.session_state[view_year_key] = preview_year
        st.session_state[view_month_key] = _month_label(preview_month)
        st.session_state[view_day_key] = preview_day
        _calendar_dialog()

    return preview_gregorian
