# Purpose: Python module in BexLogix project.
# Workflow Role: Supports operational planning and execution flow.

import sys
from pathlib import Path

# Load .env before any server config modules are imported so env vars are set.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

import streamlit as st
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TAB_ICON_PNG_PATH = PROJECT_ROOT / "client" / "assets" / "tab_icon.png"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from client import auth_state
from client.pages.login import render_forced_password_change, render_login_page
from client.pages.manager_dashboard import render_manager_dashboard
from client.pages.supervisor_dashboard import render_supervisor_dashboard
from client.pages.telesales_panel import render_telesales_panel
from client.pages.visitor_panel import render_visitor_panel
from client.styles.neumorphism import inject_global_css, render_dashboard_header
from server.app.enums.roles import UserRole
from server.app.repositories import user_repository
from server.db.database import get_db
from server.db.startup_seed import seed_if_empty

VIEW_BY_ROLE = {
    UserRole.MANAGER.value: "manager",
    UserRole.SUPERVISOR.value: "supervisor",
    UserRole.VISITOR.value: "visitor",
    UserRole.TELESALES.value: "telesales",
}


# Contract: _get_query_view executes one deterministic step in the workflow.
def _get_query_view() -> str | None:
    raw_value = st.query_params.get("view")
    if raw_value is None:
        return None
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else None
    if raw_value is None:
        return None
    normalized = str(raw_value).strip().lower()
    return normalized or None


# Contract: _set_query_view executes one deterministic step in the workflow.
def _set_query_view(view: str) -> None:
    st.query_params["view"] = view


# Contract: _validate_current_user executes one deterministic step in the workflow.
def _validate_current_user(user_payload: dict | None) -> dict | None:
    if not user_payload:
        return None

    with get_db() as db:
        db_user = user_repository.get_user_by_id(db, int(user_payload["id"]))

    if not db_user or not db_user.is_active:
        return None
    if db_user.username != user_payload["username"]:
        return None
    if db_user.role != user_payload["role"]:
        return None

    return {
        "id": int(db_user.id),
        "username": str(db_user.username),
        "role": str(db_user.role),
        "must_change_password": bool(getattr(db_user, "must_change_password", False)),
    }


def _resolve_tab_icon():
    # FIX: Use a single PNG favicon for stable browser tab rendering.
    if TAB_ICON_PNG_PATH.exists():
        try:
            return Image.open(TAB_ICON_PNG_PATH)
        except OSError:
            return str(TAB_ICON_PNG_PATH)
    return "\U0001F4E6"


# Contract: _render_topbar executes one deterministic step in the workflow.
def _render_topbar(current_user: dict, page_title: str) -> bool:
    with st.container(key="app_header_shell"):
        with st.container(key="app_header_row"):
            return render_dashboard_header(current_user, page_title)

# Contract: main executes one deterministic step in the workflow.
def main() -> None:
    st.set_page_config(
        page_title="BexLogix",
        layout="wide",
        page_icon=_resolve_tab_icon(),
        initial_sidebar_state="collapsed",
    )
    seed_if_empty()
    inject_global_css()

    requested_view = _get_query_view()
    current_user = auth_state.get_current_user()
    if current_user is None:
        current_user = auth_state.restore_session_from_query_token()

    current_user = _validate_current_user(current_user)
    if current_user is None:
        auth_state.logout_user()
        auth_state.clear_persistent_login_query()
        if requested_view != "login":
            _set_query_view("login")
            st.rerun()
        render_login_page()
        return

    auth_state.touch_session()
    expiry_warning = auth_state.get_session_expiry_warning()
    if expiry_warning:
        st.warning(expiry_warning)
        if st.button("متوجه شدم", key="ack_session_warning"):
            auth_state.acknowledge_session_expiry_warning()
            st.rerun()

    role = current_user["role"]
    expected_view = VIEW_BY_ROLE.get(role)
    if not expected_view:
        st.error("نقش کاربری پشتیبانی نمی‌شود.")
        return

    if requested_view != expected_view:
        _set_query_view(expected_view)
        st.rerun()

    if bool(current_user.get("must_change_password")):
        render_forced_password_change(current_user=current_user)
        return

    page_title = {
        UserRole.MANAGER.value: "داشبورد مدیر",
        UserRole.SUPERVISOR.value: "داشبورد سرپرست",
        UserRole.VISITOR.value: "مسیر من",
        UserRole.TELESALES.value: "پنل فروش تلفنی",
    }[role]
    if _render_topbar(current_user, page_title):
        auth_state.logout_user()
        auth_state.clear_persistent_login_query()
        _set_query_view("login")
        st.rerun()

    if role == UserRole.MANAGER.value:
        render_manager_dashboard(current_user)
    elif role == UserRole.SUPERVISOR.value:
        render_supervisor_dashboard(current_user)
    elif role == UserRole.VISITOR.value:
        render_visitor_panel(current_user)
    elif role == UserRole.TELESALES.value:
        render_telesales_panel(current_user)
    else:
        st.error("نقش کاربری پشتیبانی نمی‌شود.")


if __name__ == "__main__":
    main()

