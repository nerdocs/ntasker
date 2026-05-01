# Vendored third-party assets

All assets here are bundled to keep the tracker offline-capable.
No build step, no upgrade automation — pin versions match what was last
served via jsDelivr CDN.

## Tabler Core CSS

- Path: `tabler/tabler.min.css`
- Version: `1.0.0-beta20`
- Source: <https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/css/tabler.min.css>
- Project: <https://tabler.io>
- License: MIT — <https://github.com/tabler/tabler/blob/main/LICENSE>

## Tabler Icons (webfont)

- Paths:
  - `tabler-icons/tabler-icons.min.css`
  - `tabler-icons/fonts/tabler-icons.woff2`
  - `tabler-icons/fonts/tabler-icons.woff`
  - `tabler-icons/fonts/tabler-icons.ttf`
- Version: `3.19.0`
- Source: <https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/>
- Project: <https://tabler.io/icons>
- License: MIT — <https://github.com/tabler/tabler-icons/blob/main/LICENSE>

The CSS references font files via relative `./fonts/...` URLs, which resolve
against the CSS location — no path rewriting was needed.

## Alpine.js

- Path: `alpine/alpine.min.js`
- Version: `3.14.3`
- Source: <https://cdn.jsdelivr.net/npm/alpinejs@3.14.3/dist/cdn.min.js>
- Project: <https://alpinejs.dev>
- License: MIT — <https://github.com/alpinejs/alpine/blob/main/LICENSE.md>
