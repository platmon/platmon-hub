# Brand identity — platmon and platmon-hub

The Hub uses the **existing approved platmon C / Terminal P identity**, not a second logo. The canonical master is maintained at [platmon brand assets](https://github.com/platmon/platmon/tree/main/frontends/web/assets/brand).

The five SVG files in `hub/web/assets/brand/` are copied **byte-for-byte** from `platmon/platmon` commit [`387518146180`](https://github.com/platmon/platmon/commit/387518146180a12bfd1ccafbadef69a2d70668d4), relative path `frontends/web/assets/brand/`. They are licensed under the repositories' Apache-2.0 license. The upstream `brand-colors.json` is also included for reference but **not publicly served**.

| Item | Location / intended use |
| --- | --- |
| Light header icon | `/assets/brand/platmon-icon-light.svg` |
| Dark header icon | `/assets/brand/platmon-icon-dark.svg` selected with `prefers-color-scheme: dark` |
| Browser favicon | `/favicon.svg` (approved dark backing) |
| README horizontal logos | `hub/web/assets/brand/platmon-logo-light.svg` and `platmon-logo-dark.svg` |
| Hub web header | Approved icon only, with prominent `Fleet monitoring` heading on desktop and mobile; no visible `platmon hub` word label |

The Hub serves **only exact allowlisted** SVG paths with `image/svg+xml`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. The document CSP allows same-origin images (`img-src 'self'`) but not external image sources. No font files, external CDN, JS or dynamic asset paths are introduced.

Use the light/dark files on their corresponding page themes. Do not manually redraw `p_` using HTML text or recolor canonical SVG paths. If the upstream brand master changes, synchronize copied files explicitly and update the canonical Git blob SHA checks in `tests/test_dashboard.py` after review; never overwrite these without documenting a change in brand version.
