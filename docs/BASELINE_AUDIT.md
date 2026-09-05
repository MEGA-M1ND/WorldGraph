# Baseline Audit — God's Eye View → WorldGraph

**Audit date:** 2026-09-05
**Baseline reviewed:** [`MEGA-M1ND/gods-eye-view`](https://github.com/MEGA-M1ND/gods-eye-view)
(upstream: `bilawalsidhu/gods-eye-view`), shallow clone of `main`, 428 tracked files.
**Auditor:** WorldGraph engineering (Phase 0)
**Purpose:** decide what to reuse, modify, remove, or replace before writing WorldGraph code.

---

## 1. What God's Eye View (GEV) is

GEV is a browser-only, Vite + vanilla-JS + CesiumJS "real-time intelligence console for
planet Earth". It renders Google Photorealistic 3D Tiles and overlays ~15 live feeds
(ADS-B flights, AIS vessels, satellites, USGS earthquakes, NASA FIRMS fires, CCTV,
traffic, radio, bikeshare, military installations/flights, rocket launches, submarine
cables) plus a voice-driven OpenAI Realtime agent that can drive the camera and layers.

**There is no backend.** Every server-side concern — API-key custody, feed proxying,
rate limiting, CORS, response shaping — lives inside Vite plugin middleware in a single
**7,383-line `vite.config.js`**. That is the single most important structural fact of the
baseline, and the one WorldGraph must not inherit.

### Measured shape

| Artifact | Lines | Note |
|---|---:|---|
| `vite.config.js` | 7,383 | ~25 `/api/*` middleware proxies, key custody, rate limiting |
| `src/ui.js` | 10,293 | HUD, panels, style manager, most DOM wiring — one module |
| `style.css` | ~247 KB | single stylesheet |
| `index.html` | ~53 KB | full app markup inline |
| `src/data/*` | 190 files | one module per feed; `flights.js` alone is 5,344 lines |
| `src/voice/gevActions.js` | 3,455 | the AI tool/action runner |
| `src/data/manager.js` | 2,288 | `DataLayerManager` — the reusable core |

Unit tests are colocated `*.test.mjs` files run by a bespoke runner
(`scripts/run-unit-tests.mjs`); QA is ~40 Puppeteer scripts under `scripts/qa-*.mjs`.

---

## 2. Subsystems inspected (as required before implementation)

### 2.1 Cesium initialization — `src/main.js` (336 lines)
Creates `Cesium.Viewer` with all default chrome disabled (`timeline`, `animation`,
`baseLayerPicker`, `geocoder`, `homeButton`, `infoBox`, `selectionIndicator` all off,
`baseLayer: false`), `msaaSamples: 4`, and a hand-built `creditContainer` div appended to
`document.body` so Google's required attribution stays visible even in "clean view".
`GOOGLE_MAPS_API_KEY` is **required** — startup throws without it — and is injected into
client JS via `import.meta.env`, then re-exposed as `window.__GOOGLE_MAPS_API_KEY__` for
geocoding.

> **Finding (blocking for reuse):** the key ends up in the browser bundle. GEV accepts this
> because Google's Maps JS/Tiles keys are meant to be referrer-restricted. WorldGraph's
> security requirements (§23 of the spec: *no credentials in frontend code*) forbid the
> pattern for our own keys, and a hard failure without a paid Google key would make the
> demo non-reproducible. **WorldGraph must boot with zero credentials.**

### 2.2 `DataLayerManager` — `src/data/manager.js`
The genuinely valuable module. Responsibilities:
- `register(layerModule)` into a `Map` keyed by layer id, with a **finalization** step
  (`_registrationsFinalized`) so nothing can inject a layer late.
- Per-layer lifecycle record: `{ module, enabled, initialized, intervalId, lifecycleState,
  lifecycleUncertain }`; `AbortError` is treated as a first-class, non-error outcome.
- A **layer intent origin** concept (`user` | `voice` | `tool` | `share-restore` |
  `local-restore`) so an explicit human/agent action can cancel a pending restore.
- `layerFeedState(stats)` — normalizes heterogeneous per-layer stats into exactly one
  honest chip state: `nominal | loading | degraded | stale | fallback | unavailable`,
  with deliberate carve-outs (a "zoom in" guidance state is *not* a fault; cached data
  still reads STALE).

> **Verdict: reuse the ideas, rewrite the code.** `layerFeedState`'s honesty rules and the
> lifecycle/intent model map almost 1:1 onto WorldGraph's `WorldDataAdapter` + `FeedStatus`
> contract. The implementation itself is entangled with `renderGovernor` and
> `detection.js` and carries GEV-specific states.

### 2.3 Layer lifecycle & durable state — `src/data/layerState.js` (1,090 lines)
Encodes enabled layers + options into a compact share/localStorage token (`gev:layer-state:v2`).
Notable discipline worth copying verbatim as *policy*:
- hard ceilings on untrusted field length (`MAX_ENABLED_LAYERS_CHARS = 64`);
- **reject the whole payload on an unknown token — never salvage a prefix**;
- identity strings are grammar-checked, never truncated ("half an address is a different
  aircraft").

> **Verdict: adopt the policy** for WorldGraph share links. Do not port the codec (its
> vocabulary is GEV layers, radio filters, transponder IDs).

### 2.4 Selected-entity state / picking — `src/data/pickRegistry.js`, `scenePick.js`
Central registry mapping Cesium picked objects back to owning layers; `focusDeemphasis.js`
dims non-focused cohorts. Good pattern; GEV-specific payloads.

### 2.5 Annotations — `src/annotations/` (12 files)
Three renderers (screen-space, world-space, hybrid) behind an `annotationEngine` +
`annotationResolver`, with GeoJSON import/export. Well-factored and the closest thing in
the baseline to a clean subsystem.

> **Verdict: architectural inspiration for WorldGraph's overlay/labelling.** WorldGraph V1
> needs far less (labels + dependency paths + impact halos), so we implement a much smaller
> equivalent rather than porting 12 files.

### 2.6 Scene/camera — `src/camera.js` (76 lines), `src/scenes/director.js`, `cameraVerbs.js`
`camera.js` is a thin `flyTo` helper. `SceneDirector` + `recipes.js` + `scenePolicy.js`
compose multi-beat cinematic moves with a policy layer deciding what is allowed when.

> **Verdict: reuse the *shape*** — WorldGraph needs `focus_entity` / `focus_event` /
> `show_dependency_path` camera verbs and a "don't fight the user's camera" policy — but the
> recipes are cinematic-demo specific.

### 2.7 Realtime AI — `src/voice/gevRealtime.js` (2,610), `gevActions.js` (3,455)
Browser connects to the OpenAI Realtime API over WebRTC using an **ephemeral client secret**
minted server-side at `/api/realtime/token`; `OPENAI_API_KEY` never reaches the client.
`createGevActionRunner({ viewer, styleManager, dataManager, sceneDirector, annotations })`
returns an allowlisted action runner — the model names an action, deterministic JS executes it.

> **Verdict: this is the right trust boundary and WorldGraph keeps it.** Two changes:
> (1) WorldGraph's tools are *analysis* tools (graph traversal, blast radius, simulation),
> so they live server-side in Python next to the data, not in the browser;
> (2) `gevActions.js` is a 3,455-line switch — WorldGraph uses a typed tool registry with
> Pydantic argument schemas.

### 2.8 Server-side API proxying — `vite.config.js`
Every external call is proxied. The per-endpoint template is consistent and good:
method allowlist → opt-in per-IP rate limit → key presence check (503 with a precise
message, never a stack trace) → bounded body read (`readRequestBody(req, 64 * 1024)`) →
upstream fetch → shape the response → `Cache-Control: no-store`.

> **Verdict: reuse the *template*, replace the *host*.** WorldGraph puts these in FastAPI
> routers with Pydantic validation. Vite middleware as a credential vault does not survive
> `vite build` — GEV's production story is genuinely weaker here.

### 2.9 Share links — `src/sharelink.js` (617 lines)
`ShareLinkManager` debounces camera changes into URL params, with pluggable
`layerStateProvider` / `panelStateProvider` / `styleParamStateProvider`, a
`_restoreAuthority` guard so a restore in flight is not clobbered, and
`decodeShareCreatedAtMs` for staleness.

> **Verdict: reuse the design (providers + restore authority + debounce), rewrite for
> WorldGraph's state (incident, entity, overlays, simulation, focused path).**

### 2.10 Performance — `src/renderGovernor.js`, `worldOverlay*`
GEV uses Cesium's `requestRenderMode` behind a governor: discrete mutations call
`governorRequestRender()`; only genuine animations `holdContinuousRender()`. The earthquake
layer carries a measured comment: per-frame `CallbackProperty` ellipse axes cost
**32.4 ms/frame at 58 discs (30 fps)**; static axes cost **1.4 ms/frame (60 fps)**.

> **Verdict: adopt both the mechanism and the lesson.** WorldGraph animates dependency
> propagation, so it must hold continuous render *only during the animation* and use static
> geometry otherwise.

### 2.11 First-run — `src/firstRunExperience.js`
Explicit mission launcher instead of auto-enabling paid feeds; `?welcome=0/1` override; a
share link never sees it. Distinguishes *session* dismissal from *durable* suppression.

> **Verdict: adopt wholesale as product policy** for WorldGraph's four first-run missions.

---

## 3. Licensing findings

GEV **code** is MIT (© 2026 Bilawal Sidhu) — reuse is permitted with attribution. The
license does **not** extend to data or assets, and `DATA_SOURCES.md` documents real
carve-outs:

| Asset / source | License | Verdict for WorldGraph |
|---|---|---|
| GEV source code | MIT | ✅ May reuse with attribution. We reuse **patterns only** (see §4) — no file is copied. |
| `src/data/local_data/telegeography_submarine_cables/` | **CC BY-NC-SA 3.0** | ❌ **Do not ship.** NonCommercial is incompatible with an enterprise product. |
| `src/data/local_data/datacenters/`, `dams/` (OSM-derived) | **ODbL** (share-alike on the database) | ❌ Not shipped. WorldGraph V1 uses a **synthetic** AtlasPay estate, so no OSM database is needed. |
| `src/data/local_data/natural_earth/` | Public domain | ➖ Not needed in V1. |
| `src/data/local_data/neighborhoods/` (DataSF) | PDDL 1.0 | ➖ Not needed in V1. |
| `public/models/*` (3D models) | Per-model, mixed | ❌ Not shipped — WorldGraph has no aircraft/vessel models. |
| **Google Photorealistic 3D Tiles** | Google Maps Platform ToS, **paid key**, mandatory on-screen attribution | ❌ **Not a V1 dependency.** Makes the demo unreproducible and requires billing. Optional opt-in only. |
| **OpenSky Network** | Non-commercial; live REST use may need a written agreement | ❌ Not used. |
| **NASA FIRMS / CelesTrak / USGS** | Public / attribution-requested | ✅ USGS used (public domain, US Gov). |
| **CISA KEV** | US Government public domain | ✅ Used. |
| CesiumJS | Apache-2.0 | ✅ Used. |

**Actions taken:** no God's Eye View file, dataset, model, or tile source is copied into
WorldGraph. `THIRD_PARTY.md` records every dependency, dataset, and provider with its
license, usage, and commercial restrictions.

---

## 4. Reuse / Modify / Remove / Replace

### REUSE (as patterns, reimplemented — no code copied)
| Pattern | Source | Where it lands in WorldGraph |
|---|---|---|
| Adapter lifecycle (`initialize/start/stop/refresh/getStatus`) + registration finalization | `data/manager.js` | `backend/app/adapters/base.py`, `registry.py` |
| Honest feed-state normalization | `layerFeedState()` | `FeedState` enum + `FeedStatus` model |
| Server-side key custody + bounded body + rate limit + precise 503 | `vite.config.js` middleware template | `backend/app/api/*`, `security/ratelimit.py` |
| Allowlisted AI action runner (model names, code executes) | `voice/gevActions.js` | `backend/app/ai/tools.py` (typed registry) |
| Share-state providers + restore authority + strict codec rejection | `sharelink.js`, `layerState.js` | `frontend/src/state/sharelink.ts` |
| Render governor / static-geometry discipline | `renderGovernor.js`, `data/earthquakes.js` | `frontend/src/globe/viewer.ts` (`requestRenderMode`) |
| First-run mission launcher policy | `firstRunExperience.js` | `frontend/src/ui/firstRun.ts` |
| Cesium chrome-off viewer configuration | `main.js` | `frontend/src/globe/viewer.ts` |
| USGS `all_day.geojson` endpoint + depth banding | `data/earthquakes.js` | `backend/app/adapters/usgs.py` |

### MODIFY
- **Trust boundary moves server-side.** GEV's AI tools manipulate the browser; WorldGraph's
  compute over the graph, so they run in FastAPI with Pydantic-validated arguments and a
  strict allowlist.
- **Feed states gain provenance.** GEV tracks freshness; WorldGraph additionally tracks
  `LIVE | REPLAY | SIMULATED | SYNTHETIC` mode on every entity and event, because we mix
  synthetic enterprise data with live world signals and must never conflate them.
- **Entities become a graph, not markers.** GEV's layers own their entities. WorldGraph
  separates `WorldEntity` from `DependencyEdge` and traverses with NetworkX.

### REMOVE (deliberately not built)
Aircraft/ADS-B, AIS vessels, satellites/TLE, CCTV, radio, bikeshare, traffic tiles, rocket
launches, submarine cables, military installations/flights/awareness, thermal/noir/anime/
retro/surveillance visual styles, weather effects, cockpit view, celestial ring, scope mask,
split-flap displays, logo gaze. All are consumer/OSINT surface area irrelevant to enterprise
resilience, and several carry the licensing problems above. **Removing the military and
surveillance styling is also a product decision** (spec §14): WorldGraph is enterprise
operations software, not a military simulator.

### REPLACE
| GEV | WorldGraph |
|---|---|
| No backend; Vite middleware as API host | **FastAPI + Pydantic + SQLite + NetworkX** backend |
| Vanilla JS, 10k-line `ui.js` | **TypeScript strict**, module-per-panel frontend |
| Google Photorealistic 3D Tiles (paid, mandatory) | Cesium ellipsoid + bundled Natural-Earth-style base; Ion/Google strictly opt-in via env |
| Bespoke `.test.mjs` runner | `pytest` (backend) + `node --test` (frontend) + Playwright (E2E) |
| 247 KB single stylesheet | Design-token CSS split per surface |

---

## 5. Blockers found (and their resolutions)

1. **Hard Google Maps key requirement** would make the demo unreproducible and put a
   credential in the client bundle. → *WorldGraph boots with no credentials; imagery
   degrades gracefully; Ion/Google are opt-in.*
2. **NonCommercial bundled data (TeleGeography)** cannot ship in an enterprise product.
   → *No God's Eye View dataset is copied.*
3. **Credential custody in `vite.config.js`** does not survive a production build.
   → *All secrets live in the FastAPI backend; the frontend calls same-origin `/api/*`.*
4. **AI action layer in the browser** would let a compromised client invoke tools directly
   against feeds. → *Tools execute server-side, schema-validated and allowlisted.*

None of these make the architecture infeasible. Phase 1 proceeds.

---

## 6. Architecture decisions recorded here (ADR-style)

- **ADR-1 — Split frontend/backend.** Determinism (graph traversal, blast radius, risk,
  simulation) belongs in typed Python next to the data; the browser renders and selects.
  Cost: two toolchains. Benefit: testable engines, real secret custody, and an AI tool layer
  that cannot be driven from the client.
- **ADR-2 — No paid tile provider in the default path.** Demo reproducibility outranks
  photorealism. Google 3D Tiles remain a one-env-var opt-in.
- **ADR-3 — Deterministic analyst fallback.** The AI analyst degrades to a deterministic
  intent router when no model key is present, so the hero demo runs offline and the
  "product works without the model" requirement is satisfied literally.
- **ADR-4 — Synthetic enterprise data is always labelled.** Every entity carries
  `source.mode`; the UI renders `SYNTHETIC` / `LIVE` / `REPLAY` / `SIMULATED` badges. No code
  path may present modelled numbers as measured truth.
- **ADR-5 — Reimplement, don't fork.** GEV's MIT license permits copying, but its modules are
  entangled with subsystems WorldGraph removes. We take patterns and credit them in
  `THIRD_PARTY.md`.

---

## 7. Attribution

WorldGraph is an independent product. Architectural patterns listed in §4 are derived from
God's Eye View (MIT, © 2026 Bilawal Sidhu); the debt is recorded in `THIRD_PARTY.md`.
No God's Eye View source file, dataset, 3D model, or tile-provider configuration is
redistributed here.
