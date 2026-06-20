import streamlit as st
import hashlib
from datetime import datetime, timedelta

st.set_page_config(
    page_title="Numberball",
    page_icon="⚾",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    "<style>[data-testid='stToolbarActions'] {display: none !important;}</style>",
    unsafe_allow_html=True,
)

import extra_streamlit_components as stx

_APP_PASSWORD = st.secrets.get("app_password", "")
_COOKIE_NAME  = "numberball_auth"
_COOKIE_DAYS  = 30


def _token() -> str:
    return hashlib.sha256(_APP_PASSWORD.encode()).hexdigest()


cookie_manager = stx.CookieManager(key="auth")


def _is_authenticated() -> bool:
    if st.session_state.get("authenticated"):
        return True
    try:
        return cookie_manager.get(_COOKIE_NAME) == _token()
    except Exception:
        return False


if not _is_authenticated():
    st.title("Numberball")
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pw == _APP_PASSWORD:
            cookie_manager.set(
                _COOKIE_NAME,
                _token(),
                expires_at=datetime.now() + timedelta(days=_COOKIE_DAYS),
            )
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

st.session_state.authenticated = True

pages = [
    st.Page("pages/1_Games.py", title="Sync Data", icon="🔄"),
    st.Page("pages/2_Scouting.py", title="Scouting", icon="⚾"),
]
pg = st.navigation(pages)
pg.run()
