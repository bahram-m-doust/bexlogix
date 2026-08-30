# Purpose: Python module in BexLogix project.
# Workflow Role: Supports operational planning and execution flow.

import re
from datetime import datetime, timedelta, timezone

import streamlit as st

from client import auth_state
from client.styles.neumorphism import inject_global_css, render_login_logo
from server.app.auth.session import authenticate_user, change_password
from server.app.enums.roles import UserRole
from server.db.database import get_db

VIEW_BY_ROLE = {
    UserRole.MANAGER.value: "manager",
    UserRole.SUPERVISOR.value: "supervisor",
    UserRole.VISITOR.value: "visitor",
    UserRole.TELESALES.value: "telesales",
}

_PERSIAN_PATTERN = re.compile(r"[\u0600-\u06FF]")
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_LOCK_MINUTES = 5


# Contract: _contains_persian_text executes one deterministic step in the workflow.
def _contains_persian_text(value: str) -> bool:
    return bool(_PERSIAN_PATTERN.search(str(value or "")))


def _get_lock_state() -> tuple[bool, int]:
    lock_until_raw = st.session_state.get("login_locked_until")
    if not lock_until_raw:
        return False, 0
    try:
        lock_until = datetime.fromisoformat(str(lock_until_raw))
    except ValueError:
        st.session_state.pop("login_locked_until", None)
        return False, 0
    now = datetime.now(timezone.utc)
    remaining = int((lock_until - now).total_seconds())
    if remaining <= 0:
        st.session_state["login_attempts"] = 0
        st.session_state.pop("login_locked_until", None)
        return False, 0
    return True, remaining


def _format_remaining_time(total_seconds: int) -> str:
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes} دقیقه و {seconds} ثانیه"


def _register_failed_login_attempt() -> None:
    attempts = int(st.session_state.get("login_attempts", 0)) + 1
    st.session_state["login_attempts"] = attempts
    if attempts >= _MAX_LOGIN_ATTEMPTS:
        lock_until = datetime.now(timezone.utc) + timedelta(minutes=_LOGIN_LOCK_MINUTES)
        st.session_state["login_locked_until"] = lock_until.isoformat()


def _reset_login_rate_limit() -> None:
    st.session_state["login_attempts"] = 0
    st.session_state.pop("login_locked_until", None)


# Contract: render_login_page executes one deterministic step in the workflow.
def render_login_page() -> None:
    inject_global_css()

    is_locked, remaining_seconds = _get_lock_state()
    if is_locked:
        st.error(
            f"به دلیل ۵ تلاش ناموفق، ورود تا {_format_remaining_time(remaining_seconds)} دیگر قفل است."
        )

    with st.form("login_form", clear_on_submit=False):
        render_login_logo()
        username = st.text_input("نام کاربری", key="login_username")
        password = st.text_input("رمز عبور", type="password", key="login_password")

        if _contains_persian_text(password):
            st.warning(
                "رمز عبور را با صفحه‌کلید فارسی تایپ کرده‌اید. "
                "در صورت خطای ورود، زبان صفحه‌کلید را روی English قرار دهید."
            )

        submitted = st.form_submit_button(
            "ورود",
            use_container_width=True,
            disabled=is_locked,
            type="primary",
        )

    st.markdown(
        '<p class="login-footer-powered">Powered by <strong>Kanoon Iran Novin</strong></p>',
        unsafe_allow_html=True,
    )

    if not submitted:
        return

    with get_db() as db:
        user = authenticate_user(db, username=username, password=password)

    if not user:
        _register_failed_login_attempt()
        st.error("نام کاربری یا رمز عبور اشتباه است، یا حساب کاربر غیرفعال است.")
        return

    _reset_login_rate_limit()
    auth_state.login_user(
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
        }
    )
    auth_state.set_persistent_login_query(
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
        }
    )
    st.query_params["view"] = VIEW_BY_ROLE.get(user.role, "login")
    st.rerun()


def render_forced_password_change(current_user: dict) -> None:
    # FIX: [SEC-01] Block dashboard access until temporary password is rotated.
    render_login_logo()
    st.markdown(
        """
        <div class="login-title-block">
            <div class="login-title-main">تغییر رمز عبور الزامی است</div>
            <div class="login-title-sub">برای ادامه کار، ابتدا رمز عبور جدید تعیین کنید.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("force_change_password_form"):
        current_password = st.text_input("رمز عبور فعلی", type="password")
        new_password = st.text_input("رمز عبور جدید", type="password")
        confirm_password = st.text_input("تکرار رمز عبور جدید", type="password")
        submitted = st.form_submit_button("ذخیره رمز جدید", use_container_width=True)

    if not submitted:
        return
    if new_password != confirm_password:
        st.error("رمز عبور جدید و تکرار آن یکسان نیست.")
        return

    try:
        with get_db() as db:
            change_password(
                db=db,
                user_id=int(current_user["id"]),
                current_password=current_password,
                new_password=new_password,
            )
        st.success("رمز عبور با موفقیت تغییر کرد. لطفاً دوباره وارد شوید.")
        auth_state.logout_user()
        auth_state.clear_persistent_login_query()
        st.query_params["view"] = "login"
        st.rerun()
    except Exception as exc:
        st.error(str(exc))
