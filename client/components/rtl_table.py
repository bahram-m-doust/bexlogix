# Purpose: Shared RTL table renderer for consistent Persian data presentation.
# Workflow Role: Normalizes tabular values and renders sortable/searchable tables.

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import jdatetime
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

_VISIT_RESULT_MAP = {
    "green": "فروش انجام شده",
    "yellow": "ویزیت شد، فروش انجام نشد",
    "red": "ویزیت انجام نشد",
    "none": "ثبت نشده",
    "": "ثبت نشده",
}

_ASSIGNMENT_STATUS_MAP = {
    "draft": "پیش‌نویس",
    "supervisor_approved": "تأیید سرپرست",
    "published": "منتشرشده",
    "completed": "تکمیل‌شده",
    "skipped": "ردشده",
    "none": "ثبت نشده",
    "": "ثبت نشده",
}

_COLUMN_LABELS = {
    "assignment_id": "ردیف",
    "work_date": "تاریخ کاری",
    "visitor_code": "کد ویزیتور",
    "store_code": "کد فروشگاه",
    "store_name": "نام فروشگاه",
    "store_grade": "گرید فروشگاه",
    "route_order": "ترتیب مسیر",
    "route_distance_km": "مسافت تجمیعی مسیر (km)",
    "assignment_status": "وضعیت تخصیص",
    "visit_result": "نتیجه ویزیت",
    "followup_id": "کد پیگیری",
    "followup_date": "تاریخ پیگیری",
    "store_id": "شناسه فروشگاه",
    "store_region": "منطقه",
    "store_address": "آدرس",
    "store_lat": "عرض جغرافیایی",
    "store_lon": "طول جغرافیایی",
    "visit_id": "شناسه ویزیت",
    "visit_date": "تاریخ ویزیت",
    "visit_note": "یادداشت ویزیت",
    "unavailable_reason": "علت عدم انجام ویزیت",
    "note": "یادداشت",
    "result": "نتیجه",
}

_EN_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"


# Contract: _to_fa_digits converts English digits to Persian glyphs.
def _to_fa_digits(value: str | int) -> str:
    return str(value).translate(_EN_TO_FA_DIGITS)


# Contract: _normalize_value normalizes null-like values for display.
def _normalize_value(value: Any) -> str:
    if value is None:
        return "ثبت نشده"
    if pd.isna(value):
        return "ثبت نشده"
    text = str(value).strip()
    if text.lower() in {"", "none", "nan", "null"}:
        return "ثبت نشده"
    return text


# Contract: _to_jalali_display converts Gregorian-like values to Jalali yyyy/mm/dd.
def _to_jalali_display(value: Any) -> str:
    if value is None or pd.isna(value):
        return "ثبت نشده"

    g_date: date | None = None
    if isinstance(value, pd.Timestamp):
        g_date = value.date()
    elif isinstance(value, datetime):
        g_date = value.date()
    elif isinstance(value, date):
        g_date = value
    else:
        parsed = pd.to_datetime(str(value), errors="coerce")
        if pd.notna(parsed):
            g_date = parsed.date()

    if g_date is None:
        return _normalize_value(value)

    j_date = jdatetime.date.fromgregorian(date=g_date)
    return f"{_to_fa_digits(j_date.year)}/{_to_fa_digits(f'{j_date.month:02d}')}/{_to_fa_digits(f'{j_date.day:02d}')}"


# Contract: _localize_date_columns applies Jalali formatting on known date columns.
def _localize_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    localized = df.copy()
    for column in ("work_date", "followup_date", "visit_date"):
        if column in localized.columns:
            localized[column] = localized[column].apply(_to_jalali_display)
    return localized


# Contract: _map_status_columns localizes known enum-like status values.
def _map_status_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()

    for column in ("visit_result", "result"):
        if column in normalized.columns:
            normalized[column] = normalized[column].apply(
                lambda item: _VISIT_RESULT_MAP.get(str(item).strip().lower(), _normalize_value(item))
            )

    if "assignment_status" in normalized.columns:
        normalized["assignment_status"] = normalized["assignment_status"].apply(
            lambda item: _ASSIGNMENT_STATUS_MAP.get(str(item).strip().lower(), _normalize_value(item))
        )

    return normalized


# Contract: _rename_columns applies Persian headers for known columns.
def _rename_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = df.copy()
    applicable = {col: _COLUMN_LABELS[col] for col in renamed.columns if col in _COLUMN_LABELS}
    if applicable:
        renamed = renamed.rename(columns=applicable)
    return renamed


# Contract: _sanitize_df enforces string-safe cells for embedded HTML rendering.
def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    sanitized = df.copy()
    for col in sanitized.columns:
        sanitized[col] = sanitized[col].apply(_normalize_value)
    return sanitized


# Contract: _slugify converts arbitrary keys to DOM-safe IDs.
def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", value)


# Contract: _build_local_font_face_css embeds local Vazirmatn files into iframe CSS.
def _build_local_font_face_css() -> str:
    # FIX: [OFFLINE-02] Table iframe uses local embedded font instead of CDN.
    css_blocks: list[str] = []
    for weight, filename in (("400", "Vazirmatn-Regular.ttf"), ("700", "Vazirmatn-Bold.ttf")):
        font_path = _FONTS_DIR / filename
        if not font_path.exists():
            continue
        encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        css_blocks.append(
            f"""
@font-face {{
  font-family: 'Vazirmatn';
  font-style: normal;
  font-weight: {weight};
  src: url(data:font/ttf;base64,{encoded}) format('truetype');
  font-display: swap;
}}"""
        )
    return "".join(css_blocks)


# Contract: _apply_search filters rows by substring search over all columns.
def _apply_search(df: pd.DataFrame, query: str) -> pd.DataFrame:
    needle = str(query or "").strip().lower()
    if not needle:
        return df
    mask = pd.Series(False, index=df.index)
    for column in df.columns:
        mask = mask | df[column].astype(str).str.lower().str.contains(needle, na=False)
    return df[mask]


# Contract: render_rtl_table renders a sortable/searchable table widget using a local iframe.
def render_rtl_table(
    df: pd.DataFrame,
    *,
    key_prefix: str,
    max_height_px: int = 420,
    enable_search: bool = True,
) -> None:
    if df.empty:
        st.info("داده‌ای برای نمایش در جدول وجود ندارد.")
        return

    normalized = _localize_date_columns(df)
    normalized = _map_status_columns(normalized)
    normalized = _rename_columns(normalized)
    normalized = _sanitize_df(normalized)

    filtered = normalized
    if enable_search:
        query = st.text_input(
            "جست‌وجو در جدول",
            key=f"{key_prefix}_search",
            placeholder="مثال: STR-101 یا زر استور",
        )
        filtered = _apply_search(normalized, query)

    row_count = int(len(filtered))
    visible_table_height = min(int(max_height_px), max(86, (min(row_count, 12) + 1) * 42))
    iframe_height = visible_table_height + 56

    payload = {
        "columns": list(filtered.columns),
        "rows": filtered.to_dict(orient="records"),
        "max_height": int(visible_table_height),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    table_id = f"bex_rtl_table_{_slugify(key_prefix)}"
    local_font_css = _build_local_font_face_css()

    # FIX: Grade sort order stays deterministic: VIP > A+ > A > B > C.
    html_block = f"""
    <style>
      {local_font_css}
      :root {{
        --bex-background: #ffffff;
        --bex-foreground: #0a0a0a;
        --bex-card: #ffffff;
        --bex-muted: #f5f5f5;
        --bex-muted-foreground: #737373;
        --bex-border: #e5e5e5;
        --bex-ring: #a3a3a3;
        --bex-radius-lg: 10px;
        --bex-shadow-xs: 0 1px 2px 0 #0000000d;
      }}
      body {{
        margin: 0;
        padding: 0;
        direction: rtl;
        font-family: Vazirmatn, IRANSansFaNum, Tahoma, Arial, sans-serif;
        background: transparent;
        color: var(--bex-foreground);
      }}
      .bex-table-root {{
        direction: rtl;
        text-align: right;
      }}
      .bex-table-caption {{
        color: var(--bex-muted-foreground);
        font-size: 0.875rem;
        margin: 0.1rem 0 0.32rem;
        text-align: right;
      }}
      .bex-table-wrap {{
        max-height: {int(visible_table_height)}px;
        overflow: auto;
        border-radius: var(--bex-radius-lg);
        border: 1px solid var(--bex-border);
        background: var(--bex-card);
        box-shadow: var(--bex-shadow-xs);
        scrollbar-width: thin;
        scrollbar-color: var(--bex-ring) var(--bex-muted);
      }}
      .bex-table-wrap::-webkit-scrollbar {{
        width: 11px;
        height: 11px;
      }}
      .bex-table-wrap::-webkit-scrollbar-track {{
        background: var(--bex-muted);
        border-radius: 999px;
      }}
      .bex-table-wrap::-webkit-scrollbar-thumb {{
        background: var(--bex-ring);
        border-radius: 999px;
        border: 2px solid var(--bex-muted);
      }}
      .bex-table-wrap::-webkit-scrollbar-thumb:hover {{
        background: var(--bex-muted-foreground);
      }}
      table.bex-rtl-table {{
        width: 100%;
        border-collapse: collapse;
        border-spacing: 0;
        direction: rtl;
        table-layout: auto;
      }}
      .bex-rtl-table th,
      .bex-rtl-table td {{
        border: 0;
        border-bottom: 1px solid var(--bex-border);
        padding: 0.5rem 0.55rem;
        text-align: center;
        vertical-align: middle;
        white-space: nowrap;
        font-size: 0.875rem;
      }}
      .bex-rtl-table th {{
        position: sticky;
        top: 0;
        z-index: 2;
        background: var(--bex-muted);
        color: var(--bex-foreground);
        font-weight: 500;
        cursor: pointer;
        user-select: none;
      }}
      .bex-rtl-table th:first-child {{ border-top-right-radius: 10px; }}
      .bex-rtl-table th:last-child {{ border-top-left-radius: 10px; }}
      .bex-sort-ind {{
        margin-right: 0.25rem;
        font-size: 0.75rem;
        color: var(--bex-muted-foreground);
      }}
      .bex-empty {{
        text-align: center;
        color: var(--bex-muted-foreground);
        padding: 1rem;
      }}
    </style>
    <div id="{table_id}" class="bex-table-root"></div>
    <script>
      (function() {{
        const payload = {payload_json};
        const root = document.getElementById("{table_id}");
        const columns = Array.isArray(payload.columns) ? payload.columns : [];
        const allRows = Array.isArray(payload.rows) ? payload.rows : [];
        const gradeOrder = {{"VIP": 5, "A+": 4, "A": 3, "B": 2, "C": 1}};

        let sortKey = null;
        let sortDir = 1;

        const toEnDigits = (v) => String(v ?? "").replace(/[۰-۹]/g, d => "۰۱۲۳۴۵۶۷۸۹".indexOf(d));
        const parseNumber = (v) => {{
          const cleaned = toEnDigits(v).replace(/[^0-9.\\-]/g, "").trim();
          if (!cleaned) return null;
          const n = Number(cleaned);
          return Number.isFinite(n) ? n : null;
        }};
        const parseJalali = (v) => {{
          const t = toEnDigits(v).trim();
          const m = t.match(/^(\\d{{3,4}})\\/(\\d{{1,2}})\\/(\\d{{1,2}})$/);
          if (!m) return null;
          return Number(m[1]) * 10000 + Number(m[2]) * 100 + Number(m[3]);
        }};

        const compareRows = (a, b, key) => {{
          const vaRaw = String((a[key] ?? "")).trim();
          const vbRaw = String((b[key] ?? "")).trim();
          const va = toEnDigits(vaRaw);
          const vb = toEnDigits(vbRaw);

          if (key === "گرید فروشگاه") {{
            const ga = gradeOrder[(va || "").toUpperCase()] ?? 0;
            const gb = gradeOrder[(vb || "").toUpperCase()] ?? 0;
            if (ga !== gb) return (ga - gb) * sortDir;
            return va.localeCompare(vb, "fa", {{ numeric: true }}) * sortDir;
          }}

          const ja = parseJalali(va);
          const jb = parseJalali(vb);
          if (ja !== null && jb !== null && ja !== jb) return (ja - jb) * sortDir;

          const na = parseNumber(va);
          const nb = parseNumber(vb);
          if (na !== null && nb !== null && na !== nb) return (na - nb) * sortDir;

          return va.localeCompare(vb, "fa", {{ numeric: true, sensitivity: "base" }}) * sortDir;
        }};

        const sortedRows = (rows) => {{
          if (!sortKey) return rows;
          return [...rows].sort((a, b) => compareRows(a, b, sortKey));
        }};

        const sortIndicator = (key) => {{
          if (sortKey !== key) return "";
          return sortDir > 0 ? "▲" : "▼";
        }};

        const setSort = (key) => {{
          if (sortKey === key) {{
            sortDir = sortDir * -1;
          }} else {{
            sortKey = key;
            sortDir = key === "گرید فروشگاه" ? -1 : 1;
          }}
          renderHeader();
          renderBody();
        }};

        const requestFrameHeight = () => {{
          const desired = Math.max(130, Math.ceil(root.scrollHeight + 8));
          if (window.Streamlit && typeof window.Streamlit.setFrameHeight === "function") {{
            window.Streamlit.setFrameHeight(desired);
            return;
          }}
          window.parent.postMessage({{
            isStreamlitMessage: true,
            type: "streamlit:setFrameHeight",
            height: desired
          }}, "*");
          window.parent.postMessage({{
            type: "streamlit:setFrameHeight",
            height: desired
          }}, "*");
        }};

        const renderHeader = () => {{
          const headersHtml = columns.map((col) => (
            `<th data-col="${{col}}">${{col}} <span class="bex-sort-ind">${{sortIndicator(col)}}</span></th>`
          )).join("");
          thead.innerHTML = `<tr>${{headersHtml}}</tr>`;
          thead.querySelectorAll("th[data-col]").forEach((th) => {{
            th.addEventListener("click", () => setSort(th.getAttribute("data-col")));
          }});
        }};

        const renderBody = () => {{
          const rows = sortedRows(allRows);
          const rowsHtml = rows.length
            ? rows.map((row) => `<tr>${{columns.map((col) => `<td>${{String(row[col] ?? "")}}</td>`).join("")}}</tr>`).join("")
            : `<tr><td colspan="${{columns.length || 1}}"><div class="bex-empty">موردی پیدا نشد.</div></td></tr>`;
          tbody.innerHTML = rowsHtml;
          requestFrameHeight();
        }};

        root.innerHTML = `
          <div class="bex-table-caption">برای مرتب‌سازی، روی عنوان هر ستون کلیک کنید.</div>
          <div class="bex-table-wrap">
            <table class="bex-rtl-table">
              <thead id="{table_id}_thead"></thead>
              <tbody id="{table_id}_tbody"></tbody>
            </table>
          </div>
        `;

        const thead = document.getElementById("{table_id}_thead");
        const tbody = document.getElementById("{table_id}_tbody");

        renderHeader();
        renderBody();
        requestFrameHeight();
      }})();
    </script>
    """

    components.html(html_block, height=iframe_height, scrolling=False)
