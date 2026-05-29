import streamlit as st

st.set_page_config(
    page_title="Numberball",
    page_icon="⚾",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    "<style>header[data-testid='stHeader'] {display: none;}</style>",
    unsafe_allow_html=True,
)

pages = [
    st.Page("pages/log_ab.py", title="Log AB", icon="📝"),
    st.Page("pages/1_Sessions.py", title="Sessions", icon="📅"),
    st.Page("pages/2_Pitcher_Scouting.py", title="Pitcher Scouting", icon="⚾"),
    st.Page("pages/3_Batter_Scouting.py", title="Batter Scouting", icon="🦇"),
]
pg = st.navigation(pages)
pg.run()
