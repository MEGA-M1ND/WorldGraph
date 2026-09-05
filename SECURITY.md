# Security

WorldGraph consumes untrusted external data, places it near a language model, and shows the
result to people making operational decisions. This document states the trust boundaries and
what enforces them.

**Reporting a vulnerability:** open a private security advisory on the repository. Please do
not open a public issue for an unpatched issue.

---

## 1. Secret handling

**No credential ever reaches the browser, with two documented exceptions.**

| Secret | Where it lives | Client-visible? |
|---|---|---|
| `WORLDGRAPH_ANTHROPIC_API_KEY` | backend process env | ❌ never |
| Feed credentials (future) | backend process env | ❌ never |
| `WORLDGRAPH_CESIUM_ION_TOKEN` | backend env, served via `/api/config` | ⚠️ yes, by necessity |
| `WORLDGRAPH_GOOGLE_MAPS_API_KEY` | backend env, served via `/api/config` | ⚠️ yes, by necessity |

The two exceptions are tile-provider tokens that Cesium requires client-side; there is no
architecture in which they are not in the bundle. Both are therefore **optional** —
WorldGraph boots and runs the entire hero demo with neither — and both should be
referrer-restricted and quota-limited at the provider. This is exactly the pattern the
baseline audit flagged as unacceptable *for our own keys*, which is why WorldGraph's own
keys are never handled this way.

`Settings.public_config()` is the single function that decides what the browser learns. It
returns booleans where a secret is involved: the frontend learns *whether* an AI analyst is
available, never the key.

**Never logged.** The JSON log formatter drops any field whose name matches
`api[_-]?key|secret|token|password|credential|authorization|bearer`, and redacts
credential-shaped *values* (`sk-…`, `Bearer …`) even under an innocent field name.
`observability/logging.py`.

**Never in a share link.** The URL codec emits only entity ids, event ids, scenario ids and
camera numbers. Asserted by `frontend/tests/sharelink.test.ts`.

`.env` is git-ignored; `.env.example` documents every variable with no values.

---

## 2. The AI trust boundary

This is the security-relevant part of the product.

```
Operator ──► Analyst ──► allowlisted tool ──► deterministic engine ──► WorldGraph data
                 ▲                                       │
                 └───────── structured result ◄──────────┘
```

The model **cannot compute and cannot act**. It can only name a tool. What that tool does is
Python that a reviewer can read.

### The registry is an allowlist

23 tools. There is no `execute`, no `shell`, no `eval`, no `http_get`, no file access, no SQL.
Their absence is the control, so it is asserted by a test
(`test_ai.py::test_registry_is_an_allowlist_with_no_escape_hatches`) rather than left to
review.

Every tool has a Pydantic argument schema with `extra="forbid"`. An unparseable call is
**refused with a message**, never coerced into something plausible. Lists are length-bounded;
strings are length-bounded.

### Only four tools mutate anything

`create_simulation`, `add_simulation_override`, `remove_simulation_override`,
`reset_simulation`. What they mutate is a what-if scenario that exists inside WorldGraph and
affects nothing real. Asserted by `test_ai.py::test_no_tool_mutates_anything_operational`.

**V1 executes no operational change.** `ResponseAction.executed` is
`Literal[False]` — the schema makes "we did it" unrepresentable, not merely discouraged.

### Tools cannot invent infrastructure

Every tool that names an entity looks it up and raises if it is absent. A model asking about
a datacenter that does not exist gets *"No entity 'x' exists in the AtlasPay world model"* —
not a plausible description. Tested for `get_entity`, `calculate_blast_radius`,
`add_simulation_override` and `focus_entity`, and end-to-end through the real UI.

### Bounded loops

The model-backed analyst runs at most 8 tool rounds per turn. A model that has not answered
by then is looping, and an unbounded loop is an unbounded bill.

### Degradation is explicit

If the model is unavailable — no key, a timeout, a 429, a rejected credential — the analyst
falls back to the deterministic router and **says why**, in a banner naming the reason and
stating that analysis and simulation are unaffected. It never silently gets worse.

---

## 3. Prompt injection

External feed text — earthquake place names, CVE descriptions, provider status messages,
supplier notes — is written by people outside this organisation. It is **data**.

Three independent defences, in order of importance:

### (a) Architectural — the real control

A successful injection can make the analyst *say* something wrong. It cannot make it *do*
something, because there is nothing to do: no tool executes an operational change, no tool
reaches outside WorldGraph's own data, and every argument is schema-validated.

This is the defence that holds when the other two fail.

### (b) Structural sanitization — `security/sanitize.py`

On ingest, every external string is:

- NFKC-normalized (folding the compatibility forms used to slip a phrase past a pattern list);
- stripped of zero-width and bidirectional-override characters, and of control characters;
- length-bounded, with truncation made visible;
- scanned for instruction-like scaffolding, which is replaced with a visible
  `[redacted-instruction-like-text]` marker.

**Neutralized visibly, never silently dropped.** An operator investigating a suspicious feed
needs to see that something tried it; the event's metadata carries a `sanitized: true` flag.

### (c) The system prompt

`ai/prompts.py` states the hierarchy explicitly: system instructions, then the operator, then
*everything else is data* — naming event descriptions, CVE text, vendor descriptions, status
messages, entity metadata and any tool field, most obviously `untrusted_description`.

Tested: `test_ai.py::TestPromptInjection` covers six attack shapes, unicode lookalikes,
zero-width smuggling, and — the one that matters — that an event whose description is a
full injection payload produces **byte-identical analysis** to a clean one.

---

## 4. External data validation

- Every adapter response is shape-checked before use. A malformed payload raises
  `AdapterError`; it does not produce a half-parsed record.
- **Unusable records are dropped, not defaulted.** A feature with an unparseable magnitude or
  a nonsense timestamp is discarded. A partially-understood earthquake is not a smaller
  earthquake; it is an unusable record, and inventing defaults would put a fictional event
  on an operator's globe.
- Response bodies are capped at 8 MB.
- All requests carry an explicit timeout (10 s default, 20 s for the KEV catalog) and bounded
  retries with exponential backoff **plus jitter** — without jitter, adapters that failed
  together retry together and hand a struggling upstream a synchronized herd.
- Adapter error messages expose only the exception *type* and a fixed explanation. An
  upstream body is exactly where a key or an internal hostname ends up. Tested.

---

## 5. Client-side rendering

Every string that may have come from a feed is rendered with `textContent`. The DOM helper
`el()` **throws** if given an `html` attribute, making it the single enforced chokepoint.
`innerHTML` does not appear anywhere in the frontend.

Share links are validated against a strict grammar. **A malformed link is rejected entirely**
— never partially applied — and the operator is told, because a half-restored view looks
deliberate and is not. Every field has a length ceiling; every id must match
`^[A-Za-z0-9_.:-]{1,192}$`; camera values are range-checked.

---

## 6. API surface

- **Rate limiting** on the endpoints that cost money (AI, 20/min) or CPU (analysis, 120/min),
  keyed on the socket peer. `X-Forwarded-For` is deliberately *not* used: a header the caller
  controls is not an identity, and trusting it would let anyone bypass the limit by rotating
  a string. Deployments behind a proxy must configure trusted-proxy handling in the ASGI
  server.
- **Strict request validation.** Every body is a Pydantic model with `extra="forbid"` and
  bounded field lengths.
- **CORS** is an explicit allowlist, defaulting to the Vite dev origin, with credentials off
  and methods limited to GET/POST/DELETE.
- **Specific errors.** "Something went wrong" is banned. A 404 names what was not found; a
  429 says when to retry; the catch-all names the failed operation and states that other
  functions remain available. Stack traces go to the log, never to the client.

### Authentication

**WorldGraph V1 has none.** It is a single-tenant demonstration of a synthetic estate.
Do not expose it to an untrusted network without putting authentication in front of it.
Enterprise RBAC and multi-tenancy are stated non-goals; the seams (per-request identity in
`api/deps.py`, per-user scoping in the repository interface) exist for when they are not.

---

## 7. Data provenance as a safety property

Every entity and event carries `DataSourceInfo.mode` — `LIVE`, `REPLAY`, `SIMULATED` or
`SYNTHETIC` — and the UI renders it beside severity on every row. This is a security property,
not a nicety: an operator acting on a simulated figure believing it measured is the most
likely way this product causes harm.

Enforced in several places at once:

- `SimulationScenario.mode` is `Literal[DataMode.SIMULATED]`;
- `BusinessImpact.disclaimer` is `Literal["MODELLED ESTIMATE"]`;
- simulation tool results carry a `SIMULATED` note the model is instructed to repeat;
- `SIMULATION MODE` is a persistent on-globe banner, and the top bar shows simulated numbers
  only while that banner is up;
- a feed serving cached data reports `DEGRADED` or `STALE`, never `LIVE`.

---

## 8. Integrations

Only two live sources, both US Government public-domain and keyless: USGS earthquakes and the
CISA KEV catalog. Neither requires an account, so neither introduces a credential.

Adding an authenticated integration means: read the key from `Settings`, call it from an
adapter in the backend, never pass it through `public_config()`, and produce user-safe error
strings. The adapter base class provides the timeout, retry, size cap and error-shaping.

---

## 9. Known limitations

Stated plainly rather than implied:

- **No authentication or authorization.** See §6.
- **The prompt-injection pattern list is not exhaustive.** It cannot be. The architectural
  control in §3(a) is what the design actually relies on.
- **Rate limiting is per-process and in-memory.** It does not survive a restart or span
  replicas.
- **The tile tokens are client-visible.** §1.
- **Attack paths are reachability, not exploitability.** WorldGraph models declared network
  adjacency; it does not test authentication, network policy, or whether an exploit works.
  Every response says so.
- **SQLite with a single guarded connection.** Correct and fast at V1 scale; not a
  concurrency story for a multi-replica deployment.
