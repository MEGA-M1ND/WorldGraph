# Reality Pass — AtlasPay Coupling Audit

**Phase 8, step 1.** Performed before any Azure code was written.

**Question:** do WorldGraph's generic engines depend on AtlasPay-specific ids, names,
region labels or metadata shapes — and what do they produce for an estate that has none?

**Method:** a word-boundary grep for every fixture term across `backend/app` and
`frontend/src`, plus an empirical probe. The probe matters more than the grep: a name in a
comment is harmless, and the serious findings turned out to be places where *no fixture
term appears at all*.

---

## 1. The probe

Four entities with no business metadata, in one Azure-shaped region, all `HealthState`
defaults — the shape a cloud import actually produces:

```python
region-southeastasia  CLOUD_REGION
aks-prod              KUBERNETES_CLUSTER   HOSTED_IN region
sql-prod              DATABASE             HOSTED_IN region
web-app               APPLICATION          HOSTED_IN region
```

### Result on the untouched baseline

```
availability per entity: region 0.900, aks 0.810, sql 0.810, web 0.810
business impact:  availability 1.0    ← every entity is at 0.81
                  customers_affected 0
                  revenue_at_risk_per_hour 0.0
                  disclaimer "MODELLED ESTIMATE"
regional_capacity: {'SOUTHEASTASIA': 1.0}
material risks:   []
```

### Result of failing the region

```
severity HIGH (57/100)
  asset_criticality        +12    ← nobody declared a criticality
  customer_facing          +25    ← nobody said anything is customer-facing
  single_point_of_failure  +20
business impact:  availability 1.0, customers 0, revenue $0
confidence uncertainties:
  "region-southeastasia has no health telemetry"
  "enterprise estate is synthetic demo data (AtlasPay)"    ← a false statement
```

**WorldGraph reported 100 % availability and $0 revenue at risk for an estate it had just
modelled as entirely degraded, scored it HIGH on two classifications nobody made, and told
the operator their live cloud inventory was synthetic demo data.**

That is the Reality Pass finding. Everything below is detail.

---

## 2. Classification

Legend: **GENERIC ENGINE BUG** (must fix) · **DEMO UI COPY** (fix, cosmetic) ·
**DEMO FIXTURE** / **TEST FIXTURE** / **ACCEPTABLE EXAMPLE** (leave alone).

### GENERIC ENGINE BUG — fixed in this phase

| # | Location | Finding | Why it is a bug |
|---|---|---|---|
| **B1** | `analysis/business_impact.py::business_impact` | Organisation availability is the traffic-weighted mean over `CUSTOMER_REGION` entities. With none, `total_weight == 0` and the function **returns 1.0**. | The worst finding. An imported estate has no customer regions, so WorldGraph reports perfect availability for a world it has just modelled as broken. A fabricated reassurance. |
| **B2** | `analysis/business_impact.py::business_impact` | `customers_affected`, `revenue_at_risk_per_hour` return `0` when no metadata exists. | `0` is a measurement — "we checked, nothing". The truth is "we have no idea". Directly violates Phase 8 §24. |
| **B3** | `models/core.py::BusinessProfile` | `traffic_share=0.0`, `revenue_per_hour=0.0`, `customer_count=0`, `redundancy=1`, `capacity=1.0` are non-null defaults. | An adapter that knows nothing is forced to assert five business facts. There is no representable difference between "zero revenue" and "revenue unknown". |
| **B4** | `models/core.py::WorldEntity.criticality` | Defaults to `MEDIUM`. | An undeclared criticality becomes a business judgement WorldGraph invented, and it scores points (`+12` in the probe). |
| **B5** | `analysis/business_impact.py::is_customer_facing` | Returns `True` for any `APPLICATION` or `BUSINESS_SERVICE`. | Every Azure App Service silently becomes customer-facing. Worth `+25` risk points on evidence that does not exist. |
| **B6** | `analysis/business_impact.py::_REGION_ROLLUP` | Hardcoded `SINGAPORE/MUMBAI/TOKYO/BENGALURU → APAC`, `FRANKFURT → EMEA`, `VIRGINIA → AMER`. | A fixture lookup table inside a generic engine. Azure regions fall through to `label.upper()`: `southeastasia → 'SOUTHEASTASIA'`, never grouped. |
| **B7** | `analysis/risk.py::assess_confidence` | Unconditionally appends `"enterprise estate is synthetic demo data (AtlasPay)"`. | States a falsehood about a real estate, in the field whose entire job is honesty. |
| **B8** | `analysis/response_plan.py` | Assumption line hardcodes `"synthetic AtlasPay data"`. | Same, in the artefact an operator would act on. |
| **B9** | `ai/prompts.py` | System prompt asserts *"The organisation in this deployment is AtlasPay, a FICTIONAL payments company"*. | Actively dangerous against a real subscription: instructs the model to describe real infrastructure as fictional. |
| **B10** | `ai/router.py::_REGION_HINTS` | Hardcoded map of six words to six fixture entity ids. | Fixture data in generic routing. "Southeast Asia" resolves to nothing for an Azure estate. |
| **B11** | `ai/router.py::_route_reachability` | `target_id = targets[0] if targets else "payments-api"` | A fixture entity id as a fallback in shipped engine code. |
| **B12** | `ai/router.py::_resolve_event` | Keyword table pairing `("taiwan","earthquake")`, `("singapore","outage")`. | Fixture-specific vocabulary; should be derived from the events actually loaded. |
| **B13** | `services/world_state.py::dashboard` | `"organization": "AtlasPay"` hardcoded. | The dashboard names the fixture regardless of what is loaded. |
| **B14** | `services/world_state.py::_load_enterprise_estate` | Always loads the AtlasPay fixture. | No concept of an alternative source. Blocks the whole phase. |
| **B15** | `analysis/material_risk.py::_concentration_risks` | Requires `traffic_share ≥ 0.25`. | Azure declares no traffic share, so **concentration risk can never fire** — the single most valuable finding for a real cloud estate is structurally invisible. |
| **B16** | `analysis/propagation.py` | `HealthState.UNKNOWN = 0.9` compounds through edges: a freshly imported, entirely healthy estate reports 0.81 availability three hops down. | An invented degradation. Health that is unknown must not read as measured impairment. |

### DEMO UI COPY — fixed, cosmetic

| # | Location | Finding |
|---|---|---|
| **C1** | `ai/tools.py` ×5, `api/routes.py` ×2 | Error strings say *"the AtlasPay world model"*. Should name the active workspace. |
| **C2** | `ai/tools.py` tool descriptions ×3 | *"Search AtlasPay entities"* — the model is told its tools only work on the fixture. |
| **C3** | `ai/router.py` ×5 | Answer prose hardcodes "AtlasPay" and Singapore/Taiwan examples. |
| **C4** | `services/world_state.py` ×5 | Timeline messages say "AtlasPay asset". |
| **C5** | `frontend/src/main.ts` | `'AtlasPay'` org fallback, hardcoded legend text, fixture-specific command suggestions. |
| **C6** | `frontend/src/ui/panels.ts` | Empty-state suggests *"What happens if Singapore goes offline?"*. |
| **C7** | `main.py` FastAPI description, `adapters/kev.py` docstring | Mention AtlasPay as *the* estate. |

### DEMO FIXTURE — correct, unchanged

`fixtures/atlaspay.py` (190 hits) and `adapters/fixtures.py` (37) are the demo. They are
*supposed* to be AtlasPay-specific. Untouched.

### TEST FIXTURE — correct, unchanged

`tests/test_graph.py::TestAtlasPayFixture` and the AtlasPay assertions across the suite
pin the demo's behaviour. They are the regression guard proving Phase 8 did not break it.

### ACCEPTABLE EXAMPLE — no change

Comments and docstrings using fixture entities to *illustrate* a rule — e.g.
`models/core.py`'s "`payments-api DEPENDS_ON postgres-singapore` means the edge's source
is `payments-api`", or `graph/world_graph.py`'s note that the estate is six layers deep.
These explain generic behaviour with a concrete example and mislead nobody.

---

## 3. The pattern

Sixteen engine bugs, and they are one mistake made sixteen times:

> **The engines were written against an estate that knows everything about itself.**

AtlasPay declares traffic share, revenue, customer counts, criticality, redundancy,
customer-facing status and health for every entity. Nothing forced the engines to
distinguish *absent* from *zero*, because nothing was ever absent. Every default is a
plausible number, and plausible numbers are indistinguishable from measured ones once
they reach a UI.

A real cloud inventory is the opposite: rich in topology, nearly silent on business
meaning. Azure knows exactly which region hosts a cluster. It does not know whether the
cluster matters, who it serves, or what it earns.

**The fix is not more defaults. It is making "unknown" representable and propagating it
honestly.** That is what the decoupling work in this phase does:

1. `BusinessProfile` business fields and `redundancy` become `| None`, where `None` means
   *not declared*.
2. `Criticality.UNKNOWN` is added, and undeclared criticality no longer scores.
3. `is_customer_facing` returns a tri-state; type alone stops being evidence.
4. `BusinessImpact` fields become nullable with an `unknown_reasons` list naming each gap.
5. Region rollup is derived, not tabulated.
6. Confidence, response-plan assumptions and the system prompt are workspace-derived.
7. Concentration risk gets a count-based path for estates with no traffic data.
8. Health `UNKNOWN` stops compounding into fabricated degradation.

**AtlasPay declares all of these, so its behaviour is unchanged** — the test suite is the
proof, and it must stay green with no assertion loosened.

---

## 4. What was checked and found clean

- **`graph/world_graph.py`** — traversal, cycle breaking, bounds and truncation contain no
  fixture assumption. Comments only.
- **`analysis/propagation.py`** — the solver itself is fully generic; the only issue is the
  `UNKNOWN` health constant it consumes (B16).
- **`analysis/blast_radius.py`**, **`simulation/engine.py`** — generic. Both operate on
  whatever graph they are handed.
- **`geo/spatial.py`** — pure arithmetic.
- **`security/sanitize.py`** — source-agnostic; already the right shape for Azure tags.
- **`storage/repository.py`** — generic, though it has no workspace dimension (addressed
  under B14).
- **`WorldEntity.id` validation** — rejects `/`, so Azure resource IDs must be slugged by
  the adapter. Correct as designed: ids reach URLs.

---

## 5. Consequence for Phase 8

The Azure adapter cannot be written first. An adapter that had to supply
`traffic_share=0.0` and `criticality=MEDIUM` to satisfy the model would be **manufacturing
the exact fabrications this phase exists to eliminate** — and the fabrication would be
invisible, because it would look like ordinary field population.

Order of work:

1. Make unknown representable (B3, B4, B5) and stop the engines inventing (B1, B2, B7, B8,
   B15, B16).
2. Introduce workspaces (B14) so two estates can coexist without contaminating each other.
3. Derive what was tabulated (B6, B9–B13) and fix the copy (C1–C7).
4. *Then* write the Azure adapter, which can now say "I don't know" in the type system.

Findings are carried into `docs/REALITY_PASS_REPORT.md` with their resolutions.
