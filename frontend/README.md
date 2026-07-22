# metsuke dashboard v2 — frontend

The v2 dashboard is a **client-rendered TypeScript/Preact app** fed by a JSON API. This
directory is the *source*. The **built bundle is committed** to
`../src/metsuke/dashboard2/assets/` (`app.js` + `app.css`) and is what ships — the metsuke
runtime is **uv-only**, so end users never run node. Only a developer changing the frontend
needs the toolchain here.

## Rebuild

```
cd frontend
npm install          # pinned: preact, esbuild, typescript, jsdom
npm run build        # esbuild -> ../src/metsuke/dashboard2/assets/{app.js,app.css}
```

Then **commit the regenerated `assets/`** — that is the deliverable. `node_modules/` is
gitignored; the built assets are not.

## Gates (browser-free)

```
npm run typecheck    # tsc --noEmit, strict
npm run build        # deterministic esbuild bundle
npm test             # node --test: loads the built bundle into jsdom, stubs fetch with
                     # test/fixture.json, mounts the app, asserts the real DOM renders
```

`test/fixture.json` is generated from the **real** Python serializer so its shape cannot
drift from `to_jsonable(OverviewModel)`:

```
../.venv/bin/python ../scripts/gen_v2_fixture.py
```

These gates prove the app renders correct data and is CSP-safe. They do **not** prove it
looks good or that hover/sort/drill feel right — a human verifies appearance in a browser.

## Architecture / CSP

- **Preact** components + **hand-rolled SVG** charts (no chart library, no CDN).
- **esbuild** bundles `src/main.tsx` into one self-contained IIFE; `import "./styles.css"`
  is extracted into the sibling `app.css`.
- The server enforces a strict CSP: `default-src 'none'; script-src 'self'; style-src 'self';
  connect-src 'self'; img-src data:` with **no `'unsafe-inline'`**. So the app:
  - loads only from same-origin `/v2/app.js` and styles only from `/v2/app.css`;
  - sets **no** inline `style=`, injects **no** runtime styles (`el.style.*`), uses **no**
    CSS-in-JS — all static styling is class-based in `styles.css`;
  - encodes every **dynamic** visual (bar heights, segment widths, crosshair, cost-part
    fills) as **SVG presentation attributes**, and toggles **classes** for dynamic layout;
  - talks to the server only via a same-origin `fetch` to `/v2/api/overview`.

## Server contract (implemented in `../src/metsuke/dashboard2/web.py` + `dashboard/server.py`)

| Route | Auth | Response |
| --- | --- | --- |
| `GET /v2/dashboard` | yes (401 like `/dashboard`) | data-free HTML shell (`#app` + asset links) |
| `GET /v2/app.js` / `/v2/app.css` | no | committed bundle, `text/javascript` / `text/css` |
| `GET /v2/api/overview` | yes | `application/json`, `Cache-Control: no-store` |

The URL query (`view`/`range`/`from`/`to`/`project`/`limit`) is the source of truth. The app
reads `location.search`, passes it verbatim to the API (which owns window/preset resolution,
identical to v1's `resolve_query`), and canonicalizes its own URL in place from the resolved
`request` the API returns. Preset/date edits `pushState` + refetch; back/forward work.
The count field travels as **`limit`** (not `count`) so the query stays byte-compatible with
the shared server resolver and with v1 URLs.
