"""Supabase Auth helpers.

Password verification still goes through Supabase Auth at login. Session
persistence uses our own non-rotating device token (see device_sessions table)
so the browser-cookie write is idempotent and a dropped write costs nothing.
"""
from __future__ import annotations
import hashlib
import os
import secrets
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


def sign_in(email: str, password: str) -> tuple[str, str]:
    """Verify the password via Supabase Auth. Return (user_id, email). Raises on failure."""
    client = _make_client()
    resp = client.auth.sign_in_with_password({"email": email, "password": password})
    return resp.user.id, resp.user.email


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_device_session(user_id: str, email: str) -> str:
    """Mint an opaque per-device token, store only its hash, return the raw token."""
    raw = secrets.token_urlsafe(32)
    _make_client().table("device_sessions").insert(
        {"token_hash": _hash(raw), "user_id": user_id, "user_email": email}
    ).execute()
    return raw


def restore_device_session(raw: str) -> tuple[str, str] | None:
    """Look up a device token. Return (user_id, user_email) or None if unknown."""
    try:
        rows = (
            _make_client()
            .table("device_sessions")
            .select("user_id, user_email")
            .eq("token_hash", _hash(raw))
            .execute()
            .data
        )
    except Exception:
        return None
    return (rows[0]["user_id"], rows[0]["user_email"]) if rows else None


def revoke_device_session(raw: str) -> None:
    """Delete a single device session (sign out on this device only)."""
    try:
        _make_client().table("device_sessions").delete().eq(
            "token_hash", _hash(raw)
        ).execute()
    except Exception:
        pass
