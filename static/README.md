# Static assets (PWA icons)

Served at `app/static/<file>` because `enableStaticServing = true` in
`.streamlit/config.toml`. Referenced by the injection script in `app.py` and by
`manifest.json`.

Drop these three PNGs here (exact names matter):

| File                    | Size    | Used by            | Notes                                        |
|-------------------------|---------|--------------------|----------------------------------------------|
| `apple-touch-icon.png`  | 180x180 | iOS home screen    | No transparency - iOS rounds the corners.    |
| `icon-192.png`          | 192x192 | Android / manifest | Keep the logo inside the center ~80% (maskable safe zone). |
| `icon-512.png`          | 512x512 | Android / manifest | Same artwork as 192, higher res.             |

After deploying: on iOS the home-screen icon is cached hard - delete the existing
home-screen shortcut and re-add it to pick up the new icon.
