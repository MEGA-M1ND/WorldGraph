# Reality Pass — Report

**Phase 8.** The question this phase was asked:

> Can WorldGraph ingest a real enterprise environment and run its existing dependency,
> blast-radius, simulation and AI workflows without relying on AtlasPay-specific
> assumptions?

**Answer: yes for topology, partly for analysis, and not at all for business impact
unless a human declares it.** That last clause is not a gap left to fix later; it is the
correct answer, and most of this phase's work went into making WorldGraph say it out loud
instead of inventing a number.

This is an engineering record, not a summary of achievements. The findings are ordered by
what they cost, and the section that matters most is [§9](#9-what-is-still-not-real).

---

## 1. What was actually verified

| Claim | Evidence | Status |
|---|---|---|
| Inventory imports into the existing domain model | 96 tests in `backend/tests/test_azure_inventory.py` against a hand-authored Resource Graph fixture | **Verified** |
| The existing engines run on imported entities unchanged | Blast radius, risk, material risk, the full what-if flow and the analyst all exercised on the imported estate | **Verified** |
| Workspaces cannot contaminate each other | 35 tests in `backend/tests/test_workspaces.py`, both directions, at graph / repository / API / analysis layers | **Verified** |
| No dependency is fabricated | Explicit tests for shared resource group, shared region, shared naming prefix; every edge carries provenance | **Verified** |
| No secret escapes into an entity, a snapshot, or a log | Fixture contains a password, a connection string, a Key Vault secret and a service-principal secret; asserted absent from all three | **Verified** |
| Key Vault contents are never read | Secret child resources refused at normalization; vault itself inventoried | **Verified** |
| Malformed tags are rejected and reported, never guessed | 14 tag-validation tests | **Verified** |
| Tag values reach no instruction path | Prompt-injection payloads in `worldgraph.owner`, `depends_on`, and free text | **Verified** |
| The whole flow works in a real browser | 15 Playwright tests, 6 of them new, driving the workspace switch | **Verified** |
| Import scales to 5 000 resources | Performance budgets at 100 / 1 000 / 5 000 | **Verified** |
| **The live Azure connector works against a real subscription** | — | **NOT VERIFIED — see §2** |

Totals: **483 backend tests, 30 frontend unit tests, 15 end-to-end tests.** All green.

---

## 2. LIVE AZURE CONNECTOR NOT EXECUTED

**No Azure credentials were available in this environment, and no Azure subscription was
ever contacted.**

Everything in this report was validated against a **hand-authored fixture** at
`backend/tests/fixtures/azure_snapshot.json`, replayed through the same code path a live
import uses. The fixture was written to match Resource Graph's output shape — including
its awkward parts: mixed-case resource ids, deeply nested references, an unknown region,
an unsupported resource type, and secret-shaped properties.

What that does and does not establish:

**Established.** Normalization, edge inference, tag parsing, sanitization, coverage
assessment, workspace isolation, and every engine downstream of them. These are pure
functions of the resource rows, and the rows are realistic.

**Not established.** That `DefaultAzureCredential` resolves in a given deployment; that
the Resource Graph query is accepted verbatim by the service; that its paging behaves as
assumed beyond the first page; that a real estate's property trees stay inside the depth
and size bounds; that a 5 000-resource subscription returns inside the query timeout.
`fetch_resources()` — the ~20 lines that actually call Azure — is the one part of this
adapter that has never run.

The failure mode is bounded: a credential or query failure raises `AdapterError`, the
workspace is marked UNAVAILABLE with a reason, and the demo workspace is unaffected. But
"it fails safely" is not "it works", and this document will not claim otherwise.

---

## 3. The finding that shaped the phase

From the pre-Azure audit (`docs/REALITY_PASS_AUDIT.md`), on a four-entity estate with no
business metadata, entirely modelled as degraded:

```
availability per entity: region 0.900, aks 0.810, sql 0.810, web 0.810
business impact:  availability 1.0
                  customers_affected 0
                  revenue_at_risk_per_hour 0.0
material risks:   []
```

**100 % availability and $0 at risk, for an estate WorldGraph had just modelled as
entirely down.**

One root cause, in sixteen places: *absent and zero shared a representation.* Every
aggregate was computed by summing over declared metadata, and an estate that declares
nothing summed to zero — which is indistinguishable, in a float, from a real measurement
of nothing at stake. The engines had been written against an estate that knows everything
about itself, and had never met one that does not.

This is why Azure could not be written first. An adapter feeding a model with no way to
express "I don't know" would have had to choose a lie: `criticality=LOW`,
`customer_facing=False`, `revenue=0`. Every one of those reads as a finding.

The fix was to make UNKNOWN representable — nullable business fields, a
`Criticality.UNKNOWN`, and an `unknown_reasons` list naming the missing input — and then
to make every consumer, down to the CSS, render it as UNKNOWN rather than as a number.

The same estate today:

```
availability (customer-experienced)  None
infrastructure_availability          0.3833
customers_affected                   None
revenue_at_risk_per_hour             None
unknown_reasons:
  - Customer-experienced availability is unknown: this workspace declares no customer
    regions, so there is no population to compute an experience for.
  - Customers affected is unknown: no entity in this workspace declares a customer count.
  - Revenue exposure is unknown: no revenue metadata is available for this workspace.
  - SLA exposure is unknown: no entity declares an SLA tier.
material risks: [HIGH — Azure West Europe concentration]
```

`infrastructure_availability` is the figure a cloud import genuinely supports: it needs
only the graph. Splitting it out is what lets WorldGraph report a real number without
inventing a customer view.

**AtlasPay is byte-identical after all of this** — risk 68.7, availability 0.90451, 41 500
customers, $381 563.02/hr, APAC capacity 69 % — pinned by exact-value regression tests in
`backend/tests/test_unknowns.py`.

---

## 4. Dependency inference: what counts as evidence

The rule is that WorldGraph creates an edge only where something *proves* one. Three
sources, each recorded on the edge:

| Edge | Evidence | Provenance | Confidence |
|---|---|---|---|
| `HOSTED_IN` region | The resource's own `location` field | `azure-resource-graph` / `resource-location-field` | 0.99 |
| `CONNECTS_TO` | An explicit resource-id reference in the resource's configuration | `azure-resource-graph` / `explicit-resource-reference` | 0.98 |
| `DEPENDS_ON` | A human's `worldgraph.depends_on` tag | `worldgraph-tag` / `user-declared` | 1.0 |

**Nothing else produces an edge.** Sharing a resource group, a region, a naming prefix or
a creation time produces exactly zero, and there is a test that asserts it
(`test_shared_resource_group_creates_nothing`).

Three deliberate calibrations:

- `HOSTED_IN` carries criticality **0.9, not 1.0.** Azure regions have availability zones
  and inventory does not say whether a resource uses them. 1.0 would assert a single point
  of failure that inventory alone cannot establish.
- `CONNECTS_TO` carries criticality **0.5.** A configuration reference proves a connection
  exists; it says nothing about how much of the source's function depends on it.
- `DEPENDS_ON` is the only edge at 1.0, and only a human can create one. Inventory can
  never justify it.

References are collected by walking the whole property tree rather than a fixed path
list, because Azure nests them differently per resource type — but sensitive keys are
skipped on the way down, so a resource id inside a connection string can never become a
dependency.

**Consequence, stated plainly:** the runtime call graph is invisible to inventory. Azure
knows a web app has an App Service Plan; it does not know the app calls a payments API
every 40 ms. On the test fixture that shows as three dependency edges for seven resources
— coverage `PARTIAL` — and the remedy the UI prints is to declare them with tags or
connect a tracing source. WorldGraph does not fill that gap by guessing, which means an
imported blast radius is *narrower* than reality, not wider. That is the right direction
to be wrong in, and the coverage panel says so rather than letting the operator assume
completeness.

---

## 5. Security properties, and how each is enforced

None of these rest on intent; each has a test, and several have a CI job.

**Read-only, structurally.** The adapter imports exactly one Azure client
(`ResourceGraphClient`) and calls exactly one method (`client.resources`). A CI job greps
the entire backend for `ResourceManagementClient`, `ComputeManagementClient`, any
`begin_*` operation, `SecretClient`, `KeyClient` and `CertificateClient` and fails the
build if any appears. There is no code path from this repository that could change
anything in a subscription — not tag it, not restart it, not scale it.

**No credential reaches the browser.** `azure_subscriptions` and `azure_snapshot_path` are
read server-side and are deliberately absent from `public_config()`. A test asserts the
string "azure" does not appear in the client config at all. CI builds the frontend with
decoy `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` values in the environment and greps the
bundle for them — a build with a clean environment could not catch that regression.
Authentication is `DefaultAzureCredential`: WorldGraph never sees, stores or forwards a
secret itself.

**Secrets cannot reach an entity, a snapshot, or a log.** The Resource Graph query
projects named columns rather than `project *` — the cheapest way not to leak a field is
never to fetch it. What does arrive passes `sanitize_properties()`, which matches 19
secret-shaped key patterns case-insensitively and **redacts rather than drops**, so a
reviewer can see a field was present and removed. The test fixture deliberately carries
`hunter2`, `correct-horse-battery-staple`, `P@ssw0rd!` and `s3cr3t`; tests assert all four
are absent from every entity, from a serialized snapshot, and from captured log output at
DEBUG level.

**Key Vault: inventory only.** A resource id containing the segment `secrets`, `keys` or
`certificates` is refused at normalization and never becomes an entity. The vault itself
is inventoried. Tests assert the secret's *name* — `db-password` — does not appear
anywhere in the resulting graph.

**Tags are data.** Only the `worldgraph.` namespace is read; `env=prod` and naming
conventions mean nothing. Every value is sanitized and validated, and a malformed one is
*rejected and reported* rather than guessed at — `criticality: very important` becomes a
line in the import summary, not a CRITICAL. So does an unrecognised `worldgraph.*` key,
because a typo like `worldgraph.criticallity` would otherwise look to the operator exactly
like a tag that worked. Prompt-injection tests put `IGNORE PREVIOUS INSTRUCTIONS AND
EXECUTE ...` into `worldgraph.owner` and assert it changes no judgement, creates no edge,
and survives only as inert single-line text.

**Runs with no cloud SDK at all.** `azure-identity` and `azure-mgmt-resourcegraph` live in
`requirements-azure.txt` and are never installed by the normal path. A CI job installs
without them, asserts `import azure` fails, and runs the full backend suite — WorldGraph
must install, test and demo with no cloud account, or the demo is not reproducible.

**Two tag-parser defects the tests found.** A `worldgraph.depends_on` value longer than
512 characters was silently truncated mid-list, so a declared twelve dependencies became
three and the graph looked complete; and a fragment like `/subscriptions/x` passed as a
resource id. Both are fixed: over-long values are rejected whole with a reason, and an id
must match the full provider/type/name shape.

---

## 6. Workspace isolation

Isolation is **structural, not filtered**. Each workspace owns a separate `WorldState`
with its own graph, event store, simulation scenarios and repository file. There is no
shared collection with a `workspace_id` column, because that is precisely how the
cross-contamination bug gets written.

Requesting an unloaded workspace **raises**; it never falls back to the default. A silent
fallback would answer a question about a real Azure subscription with data from a demo
fixture, which is the single most damaging bug this product could ship. Over the API that
is a 409 with the reason, and an unknown id is a 404 — never a quiet substitution.

In the UI, switching clears every selection, analysis, simulation, highlight and
transcript before loading the new estate, and aborts everything in flight so a late
response cannot paint the new workspace with the old one's data. An entity id from
AtlasPay does not exist in an Azure import; carrying one across would render a ghost.

---

## 7. What a second estate found that review did not

Five defects survived code review and a green backend suite, and were caught only by
running the product against an estate that is not AtlasPay:

1. **The analyst crashed on an imported estate.** The deterministic router formatted
   `dashboard['availability'] * 100` — `None` for an estate with no customer regions —
   and `POST /api/ai/ask` returned a 500. Fixed to report customer-experienced
   availability where declared and *relabelled* infrastructure availability where not.

2. **The analyst invented a facility.** "Tell me about our Reykjavik quantum datacenter"
   returned a confident description of a datacenter in Singapore, because the place-name
   resolver matched the common noun "datacenter" inside that entity's name. Place tokens
   are now filtered against the entity-type vocabulary, derived from `EntityType` rather
   than hand-listed.

3. **The first-run launcher stopped appearing.** The globe persists its camera into the
   URL on idle, and the share state was read back *after* that write landed — so
   WorldGraph mistook its own bookkeeping for a link somebody sent. The arrival state is
   now captured before anything else runs.

4. **Simulation crashed the compare endpoint.** The timeline entry formatted
   `baseline.availability * 100`, `None` for an estate with no customer regions. Same
   shape as (1), one layer away, and it survived (1)'s fix because nothing had run a
   what-if against an imported estate yet.

5. **"Newly impacted" was always empty, whatever the operator failed.** The worst of the
   five, because it did not fail loudly: the endpoint returned 200 with an empty list.
   The filter skipped any entity already below the absolute impact threshold at baseline
   — correct for an estate that declares health, and catastrophic for one that does not.
   An imported estate sits at `UNKNOWN` (0.9) and inherits less through its edges, so
   *everything* started "already degraded" and no scenario was ever credited with causing
   anything. What-if analysis ran, looked fine, and answered nothing.

   The rule is now relative: an entity is newly impacted if this scenario cost it more
   than 0.5 % availability against its own baseline. AtlasPay is byte-identical under the
   new rule — the same newly-impacted sets (17 / 13 / 19 entities), cascade counts,
   availability, customers, revenue and risk band across three scenarios, verified by
   running the probe against the stashed pre-change engine and diffing. Those counts are
   now pinned.

Two further defects came from looking at the screenshots rather than the DOM: a panel
toggled with `hidden` stayed visible because `.panel`'s `display: flex` outranks the UA
rule, and the coverage report grew unbounded until it pushed the risks and event feed off
the rail.

The lesson generalises: **the fixture that makes a demo good makes its tests weak.**
AtlasPay declares everything, so it never exercises an absent-metadata path. Every one of
these seven was invisible until a second estate existed — and three of them were found
only after the first two were fixed, because each fix let the flow run one step further
before failing. A suite of 483 tests over one fixture is not the same as two fixtures.

---

## 8. Graph coverage: what WorldGraph knows, and does not

Reported dimension by dimension, **never as one confidence percentage**. A single number
would average "complete hosting topology" with "nothing at all about business services"
and produce something that looks precise and carries no information. The operator needs
the *shape* of the gap, because that is what tells them which tag to add.

On the test fixture (7 modelled resources of 9 discovered):

```
HIGH     infrastructure            7 resources imported from Azure Resource Graph
HIGH     hosting                   7 of 7 resources are placed in a region
PARTIAL  application_dependencies  3 dependency edges, all from explicit references or tags
PARTIAL  business_service          3 of 7 resources declare worldgraph.service
PARTIAL  criticality               3 of 7 resources declare a criticality
LOW      customer_exposure         2 of 7 resources declare worldgraph.customer_facing
LOW      revenue                   1 of 7 resources declare a revenue figure
```

Unsupported types are counted and named, not dropped: an import that silently discarded
44 of 187 resources would leave the operator believing the graph is complete. The same
panel lists every rejected tag with its reason.

---

## 9. What is still not real

The honest list. Each of these is a real limitation, not a roadmap entry dressed up as
one.

1. **The live connector has never run** (§2). This is the largest single gap.
2. **Runtime dependencies are invisible.** Inventory cannot see a call graph. Without
   `worldgraph.depends_on` tags or a tracing source, an imported blast radius reaches only
   what hosting and explicit configuration references prove — narrower than reality.
3. **Health is always UNKNOWN on import.** Resource Graph reports existence, not health.
   WorldGraph does not poll Azure Monitor, so an imported estate has no telemetry and
   every analysis says so in its confidence uncertainties.
4. **Region coordinates are approximations, and a wrong region gets none.** An Azure
   region is a set of datacenters across a metropolitan area; the coordinates are the
   published geography, rounded to ~1 km, and labelled `CLOUD REGION APPROXIMATION`
   everywhere they are used. A region WorldGraph does not recognise gets **no** marker
   rather than a guessed one. Consequently an earthquake near a region's map point
   establishes *potential geographic exposure* — a reason to check — and never "Azure
   region down". Only official service-health information is operational evidence, and
   WorldGraph holds no service-health connector.
5. **18 Azure resource types are modelled.** Deliberately few: mapping every type would
   produce entities no engine has rules for. Everything else is counted and reported.
6. **One subscription per workspace.** No management-group or tenant-wide traversal.
7. **Business meaning comes only from tags.** There is no CMDB import, no ServiceNow, no
   cost-API integration. An untagged estate gets honest UNKNOWNs and a coverage report
   telling it what to declare — which is correct behaviour, but it is work the operator
   has to do.
8. **Performance is bounded by budget, not measured at scale.** 5 000 resources import
   inside 60 seconds in tests; the Resource Graph query itself carries a `limit 5000` and
   paging past it is unimplemented.
9. **No multi-tenancy, no authentication, no authorization.** WorldGraph is a
   single-operator tool. Anyone who can reach the API can read every workspace it has
   loaded. That is acceptable for a locally-run analysis tool and unacceptable for a
   hosted one, and nothing here should be deployed to a shared network without an
   authentication layer in front of it.
10. **Recommendations only.** WorldGraph proposes; it never executes, and it holds no
    permission that would let it. The AI cannot say "I failed over the cluster" because
    there is no tool that could.

---

## 10. If AtlasPay were deleted tomorrow, how much of WorldGraph would still be a real product?

The question asked for an unflattering answer, so here is the honest split.

**Still real — roughly the engine, and it is the majority of the code.** The domain model,
the graph, the Jacobi propagation solver that separates availability from capacity, blast
radius with walkable explanation paths, the risk score with its full derivation, the
simulation engine's never-mutate-the-baseline contract, correlation, the deterministic
tool layer, provenance and data-mode labelling, workspace isolation, and the whole
inventory import path. None of that references AtlasPay, and after this phase all of it
runs correctly on an estate that declares nothing. The 483-test suite would lose its
fixture, not its subject.

**Would need replacing — the demonstration, and it is most of what makes a viewer believe
the engine.** AtlasPay supplies the events worth correlating, the multi-region topology
worth a blast radius, the traffic shares that make capacity modelling mean something, and
the declared revenue and customer counts that turn an availability number into a
sentence an executive reacts to. Delete it and WorldGraph still computes everything
correctly and says UNKNOWN to most of it.

**And that is the actual finding of this phase.** Before Phase 8, "delete AtlasPay" would
have produced a product that confidently reported 100 % availability and $0 at risk for a
burning estate — worse than useless, because it was wrong in a reassuring direction. After
Phase 8 it produces a product that reports infrastructure availability correctly,
concentration risk correctly, blast radius correctly, and answers UNKNOWN to the business
questions, naming what it would need to answer them.

The blunt version: **WorldGraph is a real resilience engine with a demo attached, not a
demo with an engine attached — but the business-impact layer everyone looks at first is
only as real as the metadata an operator is willing to declare.** WorldGraph now says that
clearly instead of covering it with a plausible number. Whether operators will actually
tag their estates is a product question this phase did not answer and cannot.

---

## 11. Reproducing this

No Azure account required.

```bash
# Backend, with the second workspace configured
cd backend
pip install -r requirements-dev.txt
WORLDGRAPH_DATABASE_PATH=":memory:" \
WORLDGRAPH_AZURE_SNAPSHOT_PATH=tests/fixtures/azure_snapshot.json \
  uvicorn app.main:app --host 127.0.0.1 --port 8000

# Frontend
cd frontend && npm ci && npm run build
npm run preview -- --host 127.0.0.1 --port 5173 --strictPort

# Tests
cd backend  && python -m pytest          # 483
cd frontend && npm test                  # 30
cd frontend && npx playwright test       # 15, real browser
```

To point it at a real subscription — which, again, has not been done — run `az login` and
set `WORLDGRAPH_AZURE_SUBSCRIPTIONS='["<subscription-id>"]'` after installing
`requirements-azure.txt`. The account needs `Reader` and nothing more; WorldGraph issues
one Resource Graph query and has no code path that could use anything else.

Screenshots of the imported estate are in `docs/screenshots/reality-pass-*.png`, captured
by the Playwright run rather than staged.
