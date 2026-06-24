"""Supabase Auth helpers - creates a fresh client per call to avoid shared session state."""
from __future__ import annotations
import os
import streamlit as st
from supabase import create_client


def _make_client():
    url = key = ""
    try:
        url = st.secrets.get("supabase_url", "")
        key = st.secrets.get("supabase_key", "")
    except Exception:
        pass
    url = url or os.environ.get("SUPABASE_URL", "")
    key = key or os.environ.get("SUPABASE_KEY", "")
    return create_client(url, key)


def sign_in(email: str, password: str) -> tuple[str, str, str]:
    """Return (user_id, refresh_token, email). Raises on failure."""
    client = _make_client()
    resp = client.auth.sign_in_with_password({"email": email, "password": password})
    return resp.user.id, resp.session.refresh_token, resp.user.email


def refresh_session(refresh_token: str) -> tuple[str, str, str] | None:
    """Return (user_id, new_refresh_token, email) or None if token is invalid/expired."""
    try:
        client = _make_client()
        resp = client.auth.refresh_session(refresh_token)
        if not resp.session:
            return None
        return resp.user.id, resp.session.refresh_token, resp.user.email
    except Exception:
        return None


def sign_out(refresh_token: str) -> None:
    """Revoke the refresh token server-side so it can't be reused."""
    try:
        client = _make_client()
        resp = client.auth.refresh_session(refresh_token)
        if resp.session:
            client.auth.sign_out()
    except Exception:
        pass
