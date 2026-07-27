"""Run the full refresh pipeline: fetch plays/teams/players from Supabase,
rebuild maturity_all_seasons.json, and splice it into maturity.html.

After this finishes, publish roster_maturity/maturity.html with Claude's
Artifact tool to push the update live.

Usage:
    python refresh_all.py
"""
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)  # st.secrets looks for .streamlit/ relative to cwd
STEPS = ["fetch_plays.py", "fetch_teams.py", "fetch_players.py", "build_data.py", "splice_artifact.py"]


def main():
    for step in STEPS:
        print(f"\n=== {step} ===")
        result = subprocess.run([sys.executable, os.path.join(HERE, step)], cwd=REPO_ROOT)
        if result.returncode != 0:
            print(f"{step} failed, stopping.")
            sys.exit(result.returncode)
    print("\nRefresh complete.")


if __name__ == "__main__":
    main()
