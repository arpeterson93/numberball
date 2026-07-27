"""Inject chart_script.js + data/maturity_all_seasons.json into maturity.html
(replaces the entire <script>...</script> block in place).

Run build_data.py first so data/maturity_all_seasons.json is current.

Usage:
    python splice_artifact.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "maturity.html")
SCRIPT_PATH = os.path.join(HERE, "chart_script.js")
DATA_PATH = os.path.join(HERE, "data", "maturity_all_seasons.json")


def main():
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    with open(SCRIPT_PATH, encoding="utf-8") as f:
        script = f.read()

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    seasons_json = json.dumps(data["seasons"], separators=(",", ":"))
    players_json = json.dumps(data["players"], separators=(",", ":"))
    team_totals_json = json.dumps(data["team_totals"], separators=(",", ":"))
    script = script.replace("__SEASONS_JSON__", seasons_json)
    script = script.replace("__PLAYERS_JSON__", players_json)
    script = script.replace("__TEAM_TOTALS_JSON__", team_totals_json)

    pattern = re.compile(r"<script>.*?</script>", re.DOTALL)
    new_html, n = pattern.subn(lambda m: "<script>\n" + script + "\n</script>", html, count=1)
    if n != 1:
        raise RuntimeError(f"expected exactly 1 <script> block in {HTML_PATH}, found {n}")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"spliced {len(new_html):,} bytes into {HTML_PATH}")
    print("Now publish with the Artifact tool (file_path = this maturity.html) to update the live page.")


if __name__ == "__main__":
    main()
