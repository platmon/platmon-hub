# Brand assets and provenance

[Documentation index](README.md) · [Development guide](development.md)

The Hub uses the existing approved platmon C / Terminal P identity. The canonical master is maintained at [platmon brand assets](https://github.com/platmon/platmon/tree/main/frontends/web/assets/brand).

The five SVG files in `hub/web/assets/brand/` are copied **byte-for-byte** from `platmon/platmon` commit [`387518146180`](https://github.com/platmon/platmon/commit/387518146180a12bfd1ccafbadef69a2d70668d4), relative path `frontends/web/assets/brand/`. They are licensed under the repositories' Apache-2.0 license. The upstream `brand-colors.json` is also included for reference but is not publicly served.

## Source files and served URLs

Source paths below are relative to the repository root. Browser URLs are relative to the running Hub origin.

| Use | Source file | Browser URL |
| --- | --- | --- |
| Light header icon | [platmon-icon-light.svg](../hub/web/assets/brand/platmon-icon-light.svg) | `/assets/brand/platmon-icon-light.svg` |
| Dark header icon | [platmon-icon-dark.svg](../hub/web/assets/brand/platmon-icon-dark.svg) | `/assets/brand/platmon-icon-dark.svg` |
| Browser favicon | [favicon.svg](../hub/web/assets/brand/favicon.svg) | `/favicon.svg` |
| Light README logo | [platmon-logo-light.svg](../hub/web/assets/brand/platmon-logo-light.svg) | `/assets/brand/platmon-logo-light.svg` |
| Dark README logo | [platmon-logo-dark.svg](../hub/web/assets/brand/platmon-logo-dark.svg) | `/assets/brand/platmon-logo-dark.svg` |
| Color reference | [brand-colors.json](../hub/web/assets/brand/brand-colors.json) | Not served. |

The Hub header uses the icon with a prominent `Fleet monitoring` heading on desktop and mobile. The root README uses the horizontal logos. Dark variants are selected with `prefers-color-scheme: dark`.

## Serving and updates

Hub serves only exact allowlisted SVG paths with `image/svg+xml`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. The document CSP allows same-origin images (`img-src 'self'`). There are no external fonts, CDNs, or dynamic asset paths.

Use light/dark files on their corresponding themes. Do not redraw `p_` using HTML text or recolor canonical SVG paths. If the upstream master changes, synchronize the copied files explicitly, document the brand version, and update the Git blob SHA checks in [test_dashboard.py](../tests/test_dashboard.py) after review.
