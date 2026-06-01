import streamlit as st

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

pages = [
    st.Page("pages/1_Games.py", title="Sync Data", icon="🔄"),
    st.Page("pages/2_Pitcher_Scouting.py", title="Pitcher Scouting", icon="⚾"),
    st.Page("pages/3_Batter_Scouting.py", title="Batter Scouting", icon="🦇"),
    st.Page("pages/4_Scenarios.py", title="Scenarios", icon="🧮"),
]
pg = st.navigation(pages)
pg.run()
