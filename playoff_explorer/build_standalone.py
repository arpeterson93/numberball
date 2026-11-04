"""Builds dist/standalone.html: the explorer page (page_fragment.html, written for the Dashboard runtime) with the data from
build_data.py embedded and a small stand-in for the runtime, so it works as a plain artifact anyone can open."""
import json, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / 'dist'
src = (HERE / 'page_fragment.html').read_text(encoding='utf-8')

# split the dashboard fragment into its style, markup and script
style = re.search(r'<style>(.*?)</style>', src, re.S).group(1)
style = style.replace("  #dash-root { --dash-gutter: clamp(12px, 3vw, 32px); }\n", '')
rest = src[src.index('</style>') + len('</style>'):]
markup, script = rest.split('<script>', 1)
script = script.rsplit('</script>', 1)[0]
assert 'dash-one-writer' not in markup

sessions = json.load(open(DIST / 'team_sessions.json', encoding='utf-8'))
seasons = json.load(open(DIST / 'team_seasons.json', encoding='utf-8'))
data = json.dumps({'team_sessions': sessions, 'team_seasons': seasons}, separators=(',', ':'))

head = '''<title>MLN Playoff Odds Explorer</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600&family=Source+Serif+4:wght@400;600&display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
  /* one long scrolling report: sticky filter bar, then full-width sections of cards and charts */
  :root {
    --color-bg: #f6f7f9; --color-panel: #ffffff; --color-fg: #15191f; --color-fg-muted: #5a6371; --color-border-line: #d8dce3;
    --cds-border: #c9ced7; --cds-surface-popover: #ffffff; --cds-shadow-popover: 0 4px 14px rgba(15, 20, 30, .16);
    --cds-chart-axis: #8b93a1; --cds-chart-grid: #e3e6ec; --cds-chart-reference: #d2531b;
    --font-anthropic-sans: 'Source Sans 3', system-ui, -apple-system, 'Segoe UI', sans-serif;
    --font-anthropic-serif: 'Source Serif 4', Georgia, 'Times New Roman', serif;
    --cds-font-size-body: 15px; --cds-font-size-caption: 13px; --cds-font-size-heading: 1.3rem; --cds-font-weight-medium: 600;
    --cds-gap-sm: 6px; --cds-gap-md: 12px; --cds-gap-xl: 36px; --cds-pad-xs: 4px; --cds-pad-sm: 8px; --cds-pad-lg: 18px; --cds-radius: 6px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --color-bg: #0e1218; --color-panel: #171c24; --color-fg: #e7eaee; --color-fg-muted: #97a1b0; --color-border-line: #2a313c;
      --cds-border: #38404d; --cds-surface-popover: #1d232c; --cds-shadow-popover: 0 4px 14px rgba(0, 0, 0, .5);
      --cds-chart-axis: #6c7585; --cds-chart-grid: #232a34; --cds-chart-reference: #ff8a50; color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --color-bg: #0e1218; --color-panel: #171c24; --color-fg: #e7eaee; --color-fg-muted: #97a1b0; --color-border-line: #2a313c;
    --cds-border: #38404d; --cds-surface-popover: #1d232c; --cds-shadow-popover: 0 4px 14px rgba(0, 0, 0, .5);
    --cds-chart-axis: #6c7585; --cds-chart-grid: #232a34; --cds-chart-reference: #ff8a50; color-scheme: dark;
  }
  body { background: var(--color-bg); color: var(--color-fg); font-family: var(--font-anthropic-sans); font-size: var(--cds-font-size-body);
    padding-block: 24px 48px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--color-fg); outline-offset: 2px; }
''' + style.replace('position: sticky; top: 0;', 'position: sticky; top: env(safe-area-inset-top, 0px);') + '''</style>
'''

shim = '''
  const dash = (() => {
    const DATA = ''' + data + ''';
    const draws = [], listeners = [];
    let params = { era: 's3', rows: 'rec', align: 'sess', metric: 'diff', pcolor: 'result', scale: 'count', season: 12, result: 'all', fr: 'all' };
    const render = () => {
      listeners.forEach((f) => f());
      for (const [name, fn] of draws) for (const el of document.querySelectorAll(`[data-draw="${name}"]`)) fn(el);
    };
    return {
      colors: ['var(--cds-chart-reference)'],
      data: (id) => ({ status: 'ok', data: DATA[id] }),
      params: () => params,
      setParams(o) { params = { ...params, ...o }; render(); },
      draw: (name, fn) => draws.push([name, fn]),
      onData: (f) => listeners.push(f),
      start: render
    };
  })();
'''

out = head + markup + '<script>' + shim + script + '\n  dash.start();\n</script>\n'
(DIST / 'standalone.html').write_text(out, encoding='utf-8')
print(len(out))
