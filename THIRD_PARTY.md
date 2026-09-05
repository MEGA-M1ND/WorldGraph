# Third-Party Notices

WorldGraph's own code is [MIT](LICENSE) licensed. This file records every library, dataset,
data provider and architectural debt, with its license and any commercial restriction.

**Summary: WorldGraph ships no non-commercial, no share-alike and no attribution-encumbered
data.** The entire enterprise estate is synthetic and authored here; the only external data
consumed at runtime is US Government public-domain.

---

## 1. Runtime libraries

### Frontend

| Library | Version | License | Usage | Commercial restrictions |
|---|---|---|---|---|
| [CesiumJS](https://cesium.com/platform/cesiumjs/) | 1.145.0 | Apache-2.0 | 3D globe rendering | None. Attribution per Apache-2.0 §4. |

CesiumJS bundles **Natural Earth II** imagery (`Assets/Textures/NaturalEarthII`), which is in
the **public domain** — Natural Earth places no restrictions on use. This is WorldGraph's
default basemap precisely because it needs no account and no attribution obligation.

### Backend

| Library | Version | License | Usage |
|---|---|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | 0.118.0 | MIT | HTTP API |
| [Uvicorn](https://www.uvicorn.org/) | 0.37.0 | BSD-3-Clause | ASGI server |
| [Pydantic](https://docs.pydantic.dev/) | 2.11.9 | MIT | Domain schemas and validation |
| [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | 2.11.0 | MIT | Configuration |
| [HTTPX](https://www.python-httpx.org/) | 0.28.1 | BSD-3-Clause | Outbound HTTP |
| [NetworkX](https://networkx.org/) | 3.4.2 | BSD-3-Clause | Graph storage and algorithms |
| SQLite | stdlib | Public domain | Persistence |

All permissive. None restricts commercial use.

### Development only (not shipped)

| Library | Version | License |
|---|---|---|
| [Vite](https://vite.dev/) | 7.3.6 | MIT |
| [TypeScript](https://www.typescriptlang.org/) | 5.9.3 | Apache-2.0 |
| [vite-plugin-static-copy](https://github.com/sapphi-red/vite-plugin-static-copy) | 3.4.0 | MIT |
| [tsx](https://tsx.is/) | 4.23.13 | MIT |
| [@types/node](https://www.npmjs.com/package/@types/node) | 24.13.3 | MIT |
| [Playwright](https://playwright.dev/) | 1.63.0 | Apache-2.0 |
| [pytest](https://pytest.org/) | 8.4.2 | MIT |
| [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) | 1.2.0 | Apache-2.0 |
| [RESPX](https://lundberg.github.io/respx/) | 0.22.0 | BSD-3-Clause |
| [Ruff](https://docs.astral.sh/ruff/) | 0.14.0 | MIT |

---

## 2. Data providers

### Consumed at runtime (LIVE mode only)

| Provider | Data | License / terms | Attribution | Commercial use |
|---|---|---|---|---|
| **[USGS Earthquake Hazards Program](https://earthquake.usgs.gov/earthquakes/feed/)** | Realtime M2.5+ GeoJSON | US Government work — **public domain** (17 U.S.C. §105). No key, no registration, no rate-limit agreement. | Courtesy; WorldGraph credits USGS in the provenance panel. | ✅ Unrestricted |
| **[CISA Known Exploited Vulnerabilities](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)** | KEV catalog JSON | US Government work — **public domain**. No key. | Courtesy; credited in the provenance panel. | ✅ Unrestricted |

Both are disabled by default (`WORLDGRAPH_RUN_MODE=DEMO`). Neither requires an account, so
neither introduces a credential.

### Optional, opt-in imagery — **read before enabling**

| Provider | Terms | Default |
|---|---|---|
| **[Cesium Ion](https://cesium.com/platform/cesium-ion/)** | Free tier available; commercial use requires an appropriate Cesium plan. Attribution required. | ❌ Off |
| **[Google Photorealistic 3D Tiles](https://developers.google.com/maps/documentation/tile/3d-tiles)** | **Paid** Google Maps Platform product. Usage-billed. **On-screen attribution is mandatory** under Google's Terms of Service and must remain visible, including in recordings. | ❌ Off |

Enabling Google 3D Tiles makes you responsible for its billing and for keeping its
attribution on screen. WorldGraph does not render that attribution today because the
provider is not enabled; **if you enable it, you must add the credit line.**

### Authored here

| Dataset | Origin | License |
|---|---|---|
| The AtlasPay estate (42 entities, 56 edges) | Written for this repository | MIT, with the rest of the code |
| Replay scenarios (Taiwan earthquake, Singapore region outage, `CVE-2026-DEMO-001`) | Invented | MIT |
| City coordinates in the fixture | Common geographic knowledge — not a copyrightable dataset | — |

`CVE-2026-DEMO-001` and the `atlas-gateway` product are **fictional**. The identifier cannot
collide with a real CVE, and nothing in this repository is a claim about any real product's
security.

---

## 3. Architectural debt — God's Eye View

WorldGraph's design was informed by an audit of
**[God's Eye View](https://github.com/bilawalsidhu/gods-eye-view)** (MIT, © 2026 Bilawal
Sidhu), recorded in [`docs/BASELINE_AUDIT.md`](docs/BASELINE_AUDIT.md).

**No God's Eye View source file, dataset, 3D model, configuration or asset is copied into or
redistributed by this repository.** Its MIT license would permit it; its modules are
entangled with subsystems WorldGraph deliberately removes, so we reimplemented the ideas
instead (ADR-5).

Patterns derived from it, with thanks:

| Pattern | Where it lands |
|---|---|
| Data-layer lifecycle and registration finalization | `backend/app/adapters/base.py` |
| Honest single-state feed normalization (`layerFeedState`) | `FeedState` / `FeedStatus` |
| Server-side key custody + bounded body + precise 503 | `backend/app/api/`, `security/` |
| Allowlisted action runner — model names, code executes | `backend/app/ai/tools.py` |
| Share-state providers, restore authority, strict codec rejection | `frontend/src/state/sharelink.ts` |
| Render governor and static-geometry discipline | `frontend/src/globe/` |
| First-run mission launcher policy | `frontend/src/main.ts` |
| Chrome-off Cesium viewer configuration | `frontend/src/globe/viewer.ts` |
| USGS feed endpoint and depth banding | `backend/app/adapters/usgs.py` |

### Deliberately not carried over

Everything with a licensing problem, and everything outside the product:

| Asset | Reason |
|---|---|
| TeleGeography submarine cables | **CC BY-NC-SA 3.0 — NonCommercial.** Incompatible with an enterprise product. |
| OSM-derived datacenters and dams | **ODbL** share-alike on the derived database. Not needed — WorldGraph's estate is synthetic. |
| Natural Earth and DataSF bundles | Public domain / PDDL, but unused in V1. |
| Bundled 3D models | Mixed per-model licenses; WorldGraph renders no models. |
| Google 3D Tiles as a hard dependency | Paid, and its key lands in the browser bundle. |
| OpenSky Network | Non-commercial; live REST use may need a written agreement. |
| ADS-B, AIS, satellites, CCTV, radio, bikeshare, traffic, military layers | Consumer/OSINT surface area irrelevant to enterprise resilience. |
| Thermal / noir / surveillance visual styles | Product decision: WorldGraph is enterprise operations software, not a military simulator. |

---

## 4. Fonts

WorldGraph specifies `Inter` and `JetBrains Mono` with full system fallback stacks and
**bundles neither**. No font file is redistributed and no font CDN is contacted; on a machine
without them the UI renders in the system sans-serif and monospace, which is a deliberate
tradeoff against shipping an asset with its own license.

---

## 5. Compliance summary

| Question | Answer |
|---|---|
| Any non-commercial data? | **No** |
| Any share-alike (copyleft) data? | **No** |
| Any GPL/AGPL code? | **No** — all dependencies are MIT, BSD-3-Clause or Apache-2.0 |
| Any mandatory attribution in the default build? | **No** — Apache-2.0 notices apply to redistribution of the libraries, not the running UI |
| Any paid provider in the default build? | **No** |
| Any credential required to run the demo? | **No** |
| Any third-party dataset redistributed? | **No** |

To verify: `pip-licenses` over `backend/requirements.txt`, and `npx license-checker` in
`frontend/`.
