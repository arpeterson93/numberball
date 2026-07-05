from datetime import datetime, timedelta
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
    var _showTimer = null;
    function refresh() {
        var w = doc.querySelector('[data-testid="stStatusWidget"]');
        var active = !!(w && w.textContent.trim());
        if (active) {
            if (!_showTimer) {
                _showTimer = setTimeout(function() { el.classList.add('show'); }, 400);
            }
        } else {
            clearTimeout(_showTimer);
            _showTimer = null;
            el.classList.remove('show');
        }
    }
    new p.MutationObserver(refresh).observe(doc.body,
        {childList: true, subtree: true, characterData: true});
    refresh();
}());
</script>""",
    height=0,
)

import extra_streamlit_components as stx

import auth
import database as db

_COOKIE_NAME = "nb_device"   # holds the non-rotating device token
_COOKIE_DAYS = 400

# CookieManager round-trips cookie values back to Python through a real
# component. st.context.cookies never saw JS-written cookies in this deployment;
# this bridge is the one that actually works here.
cookie_manager = stx.CookieManager(key="auth")


def _try_restore_session() -> bool:
    """Restore a session from the device-token cookie. Returns True on success."""
    if st.session_state.pop("_logged_out", False):
        return False
    token = None
    try:
        token = cookie_manager.get(_COOKIE_NAME)
    except Exception:
        pass
    if not token:
        return False
    result = auth.restore_device_session(token)
    if not result:
        try:
            cookie_manager.delete(_COOKIE_NAME)
        except Exception:
            pass
        return False
    user_id, email = result
    st.session_state.user_id = user_id
    st.session_state.user_email = email
    st.session_state.authenticated = True
    st.session_state["_device_token"] = token
    return True


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


# ── Auth gate ─────────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    if not _try_restore_session():
        # On the very first render the CookieManager iframe hasn't communicated
        # back yet, so the token appears missing even when a valid cookie exists.
        # Stop and let the CookieManager trigger its own rerun; on that second
        # pass we either restore the session or fall through to the login form.
        if "_cookie_ready" not in st.session_state:
            st.session_state["_cookie_ready"] = True
            st.stop()
        st.title("Numberball")
        with st.form("login"):
            email    = st.text_input("Email")
            password = st.text_input("Password", type="password")
            remember = st.checkbox("Remember Me", value=True)
            submitted = st.form_submit_button("Sign In")
        if submitted:
            try:
                user_id, user_email = auth.sign_in(email, password)
                st.session_state.authenticated  = True
                st.session_state.user_id        = user_id
                st.session_state.user_email     = user_email
                if remember:
                    device_token = auth.create_device_session(user_id, user_email)
                    st.session_state["_device_token"] = device_token
                    cookie_manager.set(
                        _COOKIE_NAME, device_token,
                        expires_at=datetime.now() + timedelta(days=_COOKIE_DAYS),
                    )
                st.rerun()
            except Exception:
                st.error("Invalid email or password.")
        st.stop()

_load_preferences()

with st.sidebar:
    _email = st.session_state.get("user_email", "")
    st.caption(f"Signed in as `{_email}`")
    if st.button("Sign Out"):
        _token = st.session_state.get("_device_token")
        if not _token:
            try:
                _token = cookie_manager.get(_COOKIE_NAME)
            except Exception:
                _token = None
        if _token:
            auth.revoke_device_session(_token)
        try:
            cookie_manager.delete(_COOKIE_NAME)
        except Exception:
            pass
        st.session_state.clear()
        st.session_state["_logged_out"] = True
        st.rerun()

pages = [
    st.Page("pages/2_Scouting.py", title="Scouting", icon="⚾"),
    st.Page("pages/1_Games.py", title="Sync Data", icon="🔄"),
]
pg = st.navigation(pages)
pg.run()
