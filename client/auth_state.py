# Purpose: Python module in BexLogix project.
# Workflow Role: Supports operational planning and execution flow.

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import streamlit as st

AUTH_USER_ID_KEY = "auth_user_id"
AUTH_USERNAME_KEY = "auth_username"
AUTH_ROLE_KEY = "auth_role"
AUTH_LOGIN_AT_KEY = "auth_login_at_utc"
AUTH_LAST_SEEN_AT_KEY = "auth_last_seen_at_utc"
AUTH_QUERY_TOKEN_KEY = "auth"
AUTH_TOKEN_TTL_MINUTES = 480
_DEFAULT_AUTH_SECRET = "bexlogix-local-dev-secret-change-me"
SESSION_EXPIRY_WARNING_KEY = "session_expiry_warning_text"
SESSION_EXPIRY_WARNING_ACK_KEY = "session_expiry_warning_ack"


# Contract: login_user executes one deterministic step in the workflow.
def login_user(user_payload: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    st.session_state[AUTH_USER_ID_KEY] = user_payload["id"]
    st.session_state[AUTH_USERNAME_KEY] = user_payload["username"]
    st.session_state[AUTH_ROLE_KEY] = user_payload["role"]
    st.session_state[AUTH_LOGIN_AT_KEY] = now_iso
    st.session_state[AUTH_LAST_SEEN_AT_KEY] = now_iso
    st.session_state[SESSION_EXPIRY_WARNING_ACK_KEY] = False
    st.session_state.pop(SESSION_EXPIRY_WARNING_KEY, None)


# Contract: logout_user executes one deterministic step in the workflow.
def logout_user() -> None:
    for key in [
        AUTH_USER_ID_KEY,
        AUTH_USERNAME_KEY,
        AUTH_ROLE_KEY,
        AUTH_LOGIN_AT_KEY,
        AUTH_LAST_SEEN_AT_KEY,
        SESSION_EXPIRY_WARNING_KEY,
        SESSION_EXPIRY_WARNING_ACK_KEY,
    ]:
        if key in st.session_state:
            del st.session_state[key]


# Contract: is_session_valid executes one deterministic step in the workflow.
def is_session_valid(timeout_minutes: int = 480) -> bool:
    login_at_raw = st.session_state.get(AUTH_LOGIN_AT_KEY)
    if not login_at_raw:
        return False

    try:
        login_at = datetime.fromisoformat(login_at_raw)
    except ValueError:
        return False

    now = datetime.now(timezone.utc)
    delta_minutes = (now - login_at).total_seconds() / 60
    return delta_minutes <= timeout_minutes


def _remaining_session_minutes(timeout_minutes: int = 480) -> float:
    login_at_raw = st.session_state.get(AUTH_LOGIN_AT_KEY)
    if not login_at_raw:
        return 0.0
    try:
        login_at = datetime.fromisoformat(login_at_raw)
    except ValueError:
        return 0.0
    expire_at = login_at.timestamp() + (timeout_minutes * 60)
    remaining_seconds = expire_at - datetime.now(timezone.utc).timestamp()
    return max(remaining_seconds / 60.0, 0.0)


# Contract: touch_session executes one deterministic step in the workflow.
def touch_session() -> None:
    # FIX: [UX-08] Session expiry warning is emitted before timeout to avoid losing unsaved work.
    st.session_state[AUTH_LAST_SEEN_AT_KEY] = datetime.now(timezone.utc).isoformat()
    remaining_minutes = _remaining_session_minutes(timeout_minutes=AUTH_TOKEN_TTL_MINUTES)
    acknowledged = bool(st.session_state.get(SESSION_EXPIRY_WARNING_ACK_KEY, False))
    if 0 < remaining_minutes < 30 and not acknowledged:
        st.session_state[SESSION_EXPIRY_WARNING_KEY] = (
            f"جلسهٔ شما حدود {int(round(remaining_minutes))} دقیقه دیگر منقضی می‌شود. "
            "برای تمدید، صفحه را یک‌بار تازه‌سازی کنید؛ اطلاعات ثبت‌شده حفظ می‌شود."
        )
    else:
        st.session_state.pop(SESSION_EXPIRY_WARNING_KEY, None)


# Contract: get_current_user executes one deterministic step in the workflow.
def get_current_user() -> dict | None:
    if not is_session_valid():
        return None
    user_id = st.session_state.get(AUTH_USER_ID_KEY)
    username = st.session_state.get(AUTH_USERNAME_KEY)
    role = st.session_state.get(AUTH_ROLE_KEY)
    if user_id is None or username is None or role is None:
        return None
    return {
        "id": user_id,
        "username": username,
        "role": role,
    }


def get_session_expiry_warning() -> str | None:
    return st.session_state.get(SESSION_EXPIRY_WARNING_KEY)


def acknowledge_session_expiry_warning() -> None:
    st.session_state[SESSION_EXPIRY_WARNING_ACK_KEY] = True
    st.session_state.pop(SESSION_EXPIRY_WARNING_KEY, None)


# Contract: _get_query_param_value executes one deterministic step in the workflow.
def _get_query_param_value(name: str) -> str | None:
    raw_value = st.query_params.get(name)
    if raw_value is None:
        return None
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else None
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    return text or None


# Contract: _delete_query_param executes one deterministic step in the workflow.
def _delete_query_param(name: str) -> None:
    try:
        if name in st.query_params:
            del st.query_params[name]
    except Exception:
        try:
            st.query_params.pop(name, None)
        except Exception:
            pass


# Contract: _urlsafe_b64encode executes one deterministic step in the workflow.
def _urlsafe_b64encode(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


# Contract: _urlsafe_b64decode executes one deterministic step in the workflow.
def _urlsafe_b64decode(encoded_text: str) -> bytes:
    padding = "=" * (-len(encoded_text) % 4)
    return base64.urlsafe_b64decode((encoded_text + padding).encode("ascii"))


# Contract: _get_auth_secret executes one deterministic step in the workflow.
def _get_auth_secret() -> str:
    env_secret = os.getenv("AUTH_TOKEN_SECRET", "").strip()
    if env_secret:
        return env_secret
    try:
        secret_from_streamlit = str(st.secrets.get("AUTH_TOKEN_SECRET", "")).strip()
        if secret_from_streamlit:
            return secret_from_streamlit
    except Exception:
        pass
    return _DEFAULT_AUTH_SECRET


# Contract: _build_auth_token executes one deterministic step in the workflow.
def _build_auth_token(user_payload: dict, timeout_minutes: int = AUTH_TOKEN_TTL_MINUTES) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "uid": int(user_payload["id"]),
        "usr": str(user_payload["username"]),
        "rol": str(user_payload["role"]),
        "iat": int(now.timestamp()),
        "exp": int((now.timestamp()) + (timeout_minutes * 60)),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    payload_b64 = _urlsafe_b64encode(payload_json.encode("utf-8"))
    signature = hmac.new(
        _get_auth_secret().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


# Contract: _parse_auth_token executes one deterministic step in the workflow.
def _parse_auth_token(token: str) -> dict | None:
    try:
        payload_b64, provided_signature = token.split(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(
        _get_auth_secret().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None

    try:
        payload = json.loads(_urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return None

    exp = payload.get("exp")
    uid = payload.get("uid")
    usr = payload.get("usr")
    rol = payload.get("rol")
    if not isinstance(exp, int) or not isinstance(uid, int):
        return None
    if not isinstance(usr, str) or not usr.strip():
        return None
    if not isinstance(rol, str) or not rol.strip():
        return None

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if exp < now_ts:
        return None

    return {"id": uid, "username": usr.strip(), "role": rol.strip()}


# Contract: set_persistent_login_query executes one deterministic step in the workflow.
def set_persistent_login_query(user_payload: dict) -> None:
    st.query_params[AUTH_QUERY_TOKEN_KEY] = _build_auth_token(user_payload)


# Contract: clear_persistent_login_query executes one deterministic step in the workflow.
def clear_persistent_login_query() -> None:
    _delete_query_param(AUTH_QUERY_TOKEN_KEY)


# Contract: restore_session_from_query_token executes one deterministic step in the workflow.
def restore_session_from_query_token() -> dict | None:
    token = _get_query_param_value(AUTH_QUERY_TOKEN_KEY)
    if not token:
        return None
    payload = _parse_auth_token(token)
    if payload is None:
        clear_persistent_login_query()
        return None
    login_user(payload)
    return payload
