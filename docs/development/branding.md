# Brand assets and provenance

[Documentation index](../README.md) · [Development guide](README.md)

The Hub derives its identity from the approved platmon C / Terminal P design. The original platmon SVGs remain byte-for-byte unchanged; the Hub uses separately maintained derivative SVGs based on the approved stacked-terminal design. The canonical master is maintained at [platmon brand assets](https://github.com/platmon/platmon/tree/main/frontends/web/assets/brand).

The five SVG files in `hub/web/assets/brand/` are copied **byte-for-byte** from `platmon/platmon` commit [`387518146180`](https://github.com/platmon/platmon/commit/387518146180a12bfd1ccafbadef69a2d70668d4), relative path `frontends/web/assets/brand/`. They are licensed under the repositories' Apache-2.0 license. The upstream `brand-colors.json` is also included for reference but is not publicly served.

## Hub identity (this PR)

The Hub variant preserves the **exact original vector path data for `p` and `platmon`**.
Two solid, stacked terminal tabs behind `p` distinguish a monitoring hub from a single agent.
The `hub` suffix uses vector outlines drawn to the same heights, stroke weights, and corner treatment as the upstream wordmark, without font dependencies. The descriptor is *Connect · Monitor · Manage*.

| Element | Light | Dark |
| --- | --- | --- |
| Foreground `p` | `#2F7D5B` | `#2F7D5B` |
| Terminal tabs + underscore | `#1D2127` | `#FFFFFF` |
| `platmon` wordmark | `#1D2127` | `#E6E8EB` |
| `hub` suffix | `#2F7D5B` | `#4FB286` |
| Suggested background | `#F6F7F9` | `#121417` |

README uses `hub-logo-light.svg` / `hub-logo-dark.svg` and the web header uses
`hub-icon-light.svg` / `hub-icon-dark.svg`. The browser favicon is
`hub-favicon.svg` on a dark background, served at `/favicon.svg`.
The **dark theme's terminal tabs and cursor are white**, not mint green;
the **light theme's terminal tabs and cursor are solid dark graphite**, not white outlines.
The two original platmon wordmarks and icons remain available for comparison and reuse.

## Source files and served URLs

Source paths below are relative to the repository root. Browser URLs are relative to the running Hub origin.

| Use | Source file | Browser URL |
| --- | --- | --- |
| Light header icon | [platmon-icon-light.svg](../../hub/web/assets/brand/platmon-icon-light.svg) | `/assets/brand/platmon-icon-light.svg` |
| Dark header icon | [platmon-icon-dark.svg](../../hub/web/assets/brand/platmon-icon-dark.svg) | `/assets/brand/platmon-icon-dark.svg` |
| Original platmon favicon | [favicon.svg](../../hub/web/assets/brand/favicon.svg) | `/assets/brand/favicon.svg` |
| Hub favicon | [hub-favicon.svg](../../hub/web/assets/brand/hub-favicon.svg) | `/favicon.svg` |
| Light README logo | [platmon-logo-light.svg](../../hub/web/assets/brand/platmon-logo-light.svg) | `/assets/brand/platmon-logo-light.svg` |
| Dark README logo | [platmon-logo-dark.svg](../../hub/web/assets/brand/platmon-logo-dark.svg) | `/assets/brand/platmon-logo-dark.svg` |
| Color reference | [brand-colors.json](../../hub/web/assets/brand/brand-colors.json) | Not served. |

The Hub header uses its derivative icon with a prominent `Fleet monitoring` heading on desktop and mobile. The root README uses the Hub horizontal logos. Dark variants are selected with `prefers-color-scheme: dark`.

## Serving and updates

Hub serves only exact allowlisted SVG paths with `image/svg+xml`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. The document CSP allows same-origin images (`img-src 'self'`). There are no external fonts, CDNs, or dynamic asset paths.

Use light/dark files on their corresponding themes. Do not redraw `p_` using HTML text or recolor the copied canonical SVG paths; Hub's derivative uses the same master geometry. Do not modify the upstream `platmon-*` SVGs while editing `hub-*`. If the upstream master changes, synchronize the copied files explicitly, document the brand version, and update the Git blob SHA checks in [test_dashboard.py](../../tests/test_dashboard.py) after review.
