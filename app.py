import json
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Numberball",
    page_icon="⚾",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    "<style>"
    "[data-testid='stToolbarActions']{display:none !important;}"
    "[data-testid='stStatusWidget']{display:none !important;}"
    "</style>",
    unsafe_allow_html=True,
)

components.html(
    """<script>
(function () {
    var p = window.parent;
    if (p._nbOverlayReady) return;
    p._nbOverlayReady = true;
    var doc = p.document;
    var css = doc.createElement('style');
    css.textContent =
        '#nb-overlay{display:none;position:fixed;inset:0;z-index:999999;' +
        'background:rgba(14,17,23,0.55);backdrop-filter:blur(3px);' +
        '-webkit-backdrop-filter:blur(3px);flex-direction:column;' +
        'align-items:center;justify-content:center;gap:16px}' +
        '#nb-overlay.show{display:flex}' +
        '#nb-ring{width:44px;height:44px;border-radius:50%;' +
        'border:3px solid rgba(255,255,255,0.12);border-top-color:#085d05;' +
        'animation:nb-spin 0.8s linear infinite}' +
        '@keyframes nb-spin{to{transform:rotate(360deg)}}' +
        '#nb-lbl{color:rgba(255,255,255,0.65);' +
        'font:13px/1 -apple-system,sans-serif;letter-spacing:0.06em}';
    doc.head.appendChild(css);
    var el = doc.createElement('div');
    el.id = 'nb-overlay';
    el.innerHTML = '<div id="nb-ring"></div><div id="nb-lbl">Loading…</div>';
    doc.body.appendChild(el);
    function refresh() {
        var w = doc.querySelector('[data-testid="stStatusWidget"]');
        el.classList.toggle('show', !!(w && w.textContent.trim()));
    }
    new p.MutationObserver(refresh).observe(doc.body,
        {childList: true, subtree: true, characterData: true});
    refresh();
}());
</script>""",
    height=0,
)

import auth
import database as db

_COOKIE_KEY     = "nb_auth"
_COOKIE_MAX_AGE = 34560000  # 400 days in seconds


def _cookie_write(token: str) -> None:
    """Persist the refresh token in a long-lived browser cookie."""
    components.html(
        "<script>document.cookie="
        + json.dumps(
            f"{_COOKIE_KEY}={token}; max-age={_COOKIE_MAX_AGE}; path=/; SameSite=Strict"
        )
        + ";</script>",
        height=0,
    )


def _cookie_clear() -> None:
    """Expire the auth cookie immediately."""
    components.html(
        "<script>document.cookie="
        + json.dumps(f"{_COOKIE_KEY}=; max-age=0; path=/; SameSite=Strict")
        + ";</script>",
        height=0,
    )


def _load_preferences() -> None:
    if "scouting_view" not in st.session_state:
        user_id = st.session_state.get("user_id", "")
        if user_id:
            try:
                prefs = db.get_user_preferences(user_id)
                if not prefs:
                    db.upsert_user_preferences(user_id, "complex")
                st.session_state["scouting_view"] = prefs.get("scouting_view", "complex")
                if prefs.get("last_sheet_url"):
                    st.session_state["pred_sheet_sel"] = prefs["last_sheet_url"]
            except Exception:
                st.session_state["scouting_view"] = "complex"


# ── Session restore via cookie (Python-readable on every render) ──────────────
if not st.session_state.get("authenticated"):
    _cookie_token = st.context.cookies.get(_COOKIE_KEY, "")
    if _cookie_token:
        _result = auth.refresh_session(_cookie_token)
        if _result:
            _uid, _new_token, _email = _result
            st.session_state.authenticated  = True
            st.session_state.user_id        = _uid
            st.session_state.user_email     = _email
            st.session_state["_refresh_token"] = _new_token
            _cookie_write(_new_token)   # rotate token so the cookie stays fresh

# ── Auth gate ─────────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.title("Numberball")
    with st.form("login"):
        email    = st.text_input("Email")
        password = st.text_input("Password", type="password")
        remember = st.checkbox("Remember Me", value=True)
        submitted = st.form_submit_button("Sign In")
    if submitted:
        try:
            user_id, refresh_token, user_email = auth.sign_in(email, password)
            st.session_state.authenticated  = True
            st.session_state.user_id        = user_id
            st.session_state.user_email     = user_email
            st.session_state["_refresh_token"] = refresh_token
            if remember:
                _cookie_write(refresh_token)
            st.rerun()
        except Exception:
            st.error("Invalid email or password.")
    st.stop()

_load_preferences()

with st.sidebar:
    _email = st.session_state.get("user_email", "")
    st.caption(f"Signed in as `{_email}`")
    if st.button("Sign Out"):
        _token = st.session_state.get("_refresh_token")
        if _token:
            try:
                auth.sign_out(_token)
            except Exception:
                pass
        _cookie_clear()
        st.session_state.clear()
        st.rerun()

pages = [
    st.Page("pages/2_Scouting.py", title="Scouting", icon="⚾"),
    st.Page("pages/1_Games.py", title="Sync Data", icon="🔄"),
]
pg = st.navigation(pages)
pg.run()
