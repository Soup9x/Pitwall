# Vendored front-end libraries

Served from Pitwall itself because the Content-Security-Policy only allows same-origin scripts.

| File | Library | Version | Source | License |
|---|---|---|---|---|
| `chart.umd.min.js` | Chart.js | 4.4.9 | npm `chart.js@4.4.9`, `dist/chart.umd.js` (unmodified) | MIT, see `chart.js.LICENSE.md` |

To update: `npm pack chart.js@<version>`, copy `package/dist/chart.umd.js` here and bump the
version above.
