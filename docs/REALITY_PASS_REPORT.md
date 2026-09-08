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

> **Status.** Sections 1–11 were written at commit `c16614c`, when Phase 8 closed. An
> external review of that commit then raised eight findings; all eight held, several in code
> this report had described as verified. Those and six more found by running the app rather
> than reading it have since been fixed and merged. **[§12](#12-what-an-external-review-found-after-this-report-was-written)
> records what they were and what they say about the rest of this document.** Counts and
> claims throughout have been corrected to the current state, except where a figure is
> explicitly historical; the substance of §1–§11 is otherwise as written.
>
> §12 closes by saying that reverting a fix and watching a test go red is the only thing
> separating a test from a comment. **[§13](#13-mutation-testing-what-the-test-suite-could-not-tell-apart)
> applies that standard to the whole suite by machine** — mutating every constant,
> comparison and boolean in the code under audit and asking whether any test notices. It
> found that the nine prompt-injection patterns, the reachability factor in CVE scoring,
> and the function that *is* workspace isolation were all unprotected, and that an
> exception's text bypassed log redaction entirely. 597 tests → 1,456, 92 % → 99 % coverage.

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
| The whole flow works in a real browser | 19 Playwright tests driving the workspace switch, the analyst, and the layout at five viewport widths | **Verified** |
| Import scales to 5 000 resources | Performance budgets at 100 / 1 000 / 5 000 | **Verified** |
| Resource Graph paging is handled | 21 tests in `backend/tests/test_azure_pagination.py` against a fake client, run both with and without the optional SDK because the request shape differs: exact-multiple boundaries, a vanishing token, a token that never vanishes, an empty page mid-walk, the ceiling, and a disagreeing `total_records` | **Verified against a fake client — see §2** |
| A vulnerability count matches its evidence | 14 tests in `backend/tests/test_vulnerability_counts.py`; confirmed and product-name-only matches counted and scored apart | **Verified** |
| The model cannot state a figure it was not given | 20 tests in `backend/tests/test_answer_grounding.py`; unsourced figures with no tool calls are withheld, not returned | **Verified** |
| **The live Azure connector works against a real subscription** | — | **NOT VERIFIED — see §2** |

Totals: **597 backend tests, 32 frontend unit tests, 19 end-to-end tests.** All green.

Three of those rows are new since this report was first written, and one of them —
paging — replaced a claim that was quietly false. See [§12](#12-what-an-external-review-found-after-this-report-was-written).

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
the Resource Graph query is accepted verbatim by the service; that a real estate's
property trees stay inside the depth and size bounds; that a large subscription returns
inside the query timeout. `fetch_resources()` — the ~100 lines that actually call Azure —
is the one part of this adapter that has never run.

**Changed since this was written.** The sentence above used to include *"that its paging
behaves as assumed beyond the first page"*. That was too generous to the code: paging was
not merely unverified, it was **absent**. The query carried `| limit 5000`, Resource Graph
caps a response at 1 000 rows regardless, and the `skip_token` was never read — so a
5 000-resource subscription would have imported 1 000 resources and reported HIGH coverage
over them. Paging is now implemented and exercised by 17 tests against a fake client,
including the case where Azure's own `total_records` disagrees with the row count.

**That fix does not move this section's verdict.** A fake client is a test of the paging
loop I wrote, not of Resource Graph. `fetch_resources` takes a `client_factory` argument
purely so that loop can be driven in tests; it does not make the live path verified, and
the docstring says so at the call site.

### An attempt to run it, and what that turned up

The live path was then attempted in an environment with the real SDK installed. It has
**still never contacted Azure** — there were no credentials of any kind, and the network
policy answers `403` to `CONNECT management.azure.com:443`. What it did establish:

| Executed for the first time | Result |
|---|---|
| `pip install -r requirements-azure.txt` in a clean environment | **Failed.** `azure-mgmt-resourcegraph==8.0.0` does `from six import with_metaclass` at import time without declaring `six`, and nothing in its chain pulls it in any more |
| The error an operator would then see | *"Azure SDK is not installed. Install the optional dependencies with `pip install -r requirements-azure.txt`"* — after they had just run exactly that |
| `_request_factory()` against the real SDK | Builds a genuine `QueryRequest` with `options.top = 1000` and `options.skip_token` threaded — the first time the real model classes have accepted the shape the paging loop builds |
| `fetch_resources()` reaching `DefaultAzureCredential` | Raised `AdapterError` naming only the exception type (`ClientAuthenticationError`); the subscription id appeared in neither the message nor the logs |
| Log inspection at `INFO` and `WARNING` (the shipped default) | No request URL, tenant, or token in any line. At `DEBUG` the Azure SDK and `urllib3` emit their own request URLs — third-party output, not WorldGraph's, and absent at the level the app configures |

So the connector's **failure** path is now executed rather than assumed, and the pinned
dependency set was broken for anyone who followed the instructions in §11. Both are fixed;
a CI job now installs the optional extras and imports what the adapter imports.

**And a test was found lying.** The 17 paging tests read the continuation token with
`request.get("skip_token") if isinstance(request, dict) else None`. With the optional SDK
installed the request is a real `QueryRequest`, so that reported *no token at all* — the
one test proving the loop threads its token passed while asserting nothing. CI runs
without the SDK, so it only ever exercised the dict fallback, a shape that never reaches
Azure. The token is now read from whichever shape was built, and the suite is run both
ways.

**None of this makes the live connector verified.** A request object the SDK accepts is
not a query Resource Graph answers, and an authentication failure is not a successful
read. The verdict above is unchanged.

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

A second job now does the mirror of that, and it exists because only testing the *absence*
path let a broken pin ship: it installs `requirements-azure.txt` on its own and imports
the four symbols the adapter uses. See §2.

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
before failing. A suite of 483 tests — the entire backend suite at that point — over one fixture is not the same as two fixtures.

---

## 8. Graph coverage: what WorldGraph knows, and does not

Reported dimension by dimension, **never as one confidence percentage**. A single number
would average "complete hosting topology" with "nothing at all about business services"
and produce something that looks precise and carries no information. The operator needs
the *shape* of the gap, because that is what tells them which tag to add.

On the test fixture (7 modelled resources of 9 discovered):

```
HIGH     collection                9 of 9 resources retrieved
HIGH     infrastructure            7 resources imported from Azure Resource Graph
HIGH     hosting                   7 of 7 resources are placed in a region
PARTIAL  application_dependencies  3 dependency edges, all from explicit references or tags
PARTIAL  business_service          3 of 7 resources declare worldgraph.service
PARTIAL  criticality               3 of 7 resources declare a criticality
LOW      customer_exposure         2 of 7 resources declare worldgraph.customer_facing
LOW      revenue                   1 of 7 resources declare a revenue figure
```

`collection` leads the list, and that ordering is the point: every dimension below it is a
ratio over what was *retrieved*, and a ratio over a fraction of an estate says nothing
about the estate. When collection is incomplete the row reads `LOW` and its remedy states
plainly that the figures beneath it describe only what was retrieved. That row did not
exist when this section was first written — see [§12](#12-what-an-external-review-found-after-this-report-was-written).

Unsupported types are counted and named, not dropped: an import that silently discarded
44 of 187 resources would leave the operator believing the graph is complete. The same
panel lists every rejected tag with its reason.

---

## 9. What is still not real

The honest list. Each of these is a real limitation, not a roadmap entry dressed up as
one.

1. **The live connector has never run** (§2). This is the largest single gap, and it is
   the same gap it was when this report was written — thirteen merged fixes since have
   made the code around it better without touching the fact that it has never contacted
   Azure.
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
   inside 60 seconds in tests, from a fixture on local disk. Paging *is* now implemented
   and bounded at 20 000 resources, beyond which collection stops and reports itself
   incomplete — but no timing figure here involves a network, so none of it says what a
   real subscription costs. (This item previously ended *"the Resource Graph query itself
   carries a `limit 5000` and paging past it is unimplemented"*. That was accurate and
   its consequence was not thought through: see §12.)
9. **No multi-tenancy, no authentication, no authorization.** WorldGraph is a
   single-operator tool. Anyone who can reach the API can read every workspace it has
   loaded. That is acceptable for a locally-run analysis tool and unacceptable for a
   hosted one, and nothing here should be deployed to a shared network without an
   authentication layer in front of it.
10. **Recommendations only.** WorldGraph proposes; it never executes, and it holds no
    permission that would let it. The AI cannot say "I failed over the cluster" because
    there is no tool that could.
11. **An attack path is reachability, and part of it is inferred.** WorldGraph walks
    dependency edges backwards to model an attacker moving into what relies on a service.
    That is real for an authentication service and false for a database, and an
    operational dependency edge cannot tell them apart — so such a hop is labelled
    `INFERRED_TRUST` at low confidence and the path is scored by its weakest hop. Two
    applications that merely share a database still produce a path. Establishing this
    properly needs edges inventory cannot supply: who may assume which role, who accepts
    whose tokens, what network policy permits.
12. **A product-name match is not a finding.** Vulnerability correlation is confirmed only
    when an asset's own inventory names the CVE. A bare product-name match ignores version
    and vendor, is labelled `POTENTIALLY_AFFECTED`, and is counted and scored separately
    from confirmed matches — it is a triage candidate, not exposure.
13. **`ExposureProfile.authenticated` is `None` on import, and that is the truth.** Cloud
    inventory does not report whether a workload authenticates its callers. It used to
    default to `True`, which asserted a security property from nothing, in the direction
    that makes an estate look safer.

---

## 10. If AtlasPay were deleted tomorrow, how much of WorldGraph would still be a real product?

The question asked for an unflattering answer, so here is the honest split.

**Still real — roughly the engine, and it is the majority of the code.** The domain model,
the graph, the Jacobi propagation solver that separates availability from capacity, blast
radius with walkable explanation paths, the risk score with its full derivation, the
simulation engine's never-mutate-the-baseline contract, correlation, the deterministic
tool layer, provenance and data-mode labelling, workspace isolation, and the whole
inventory import path. None of that references AtlasPay, and after this phase all of it
runs correctly on an estate that declares nothing. The 597-test suite would lose its
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
cd backend  && python -m pytest          # 597
cd frontend && npm test                  # 32
cd frontend && npx playwright test       # 19, real browser
```

To point it at a real subscription — which, again, has not been done — run `az login` and
set `WORLDGRAPH_AZURE_SUBSCRIPTIONS='["<subscription-id>"]'` after installing
`requirements-azure.txt`. The account needs `Reader` and nothing more; WorldGraph issues
one Resource Graph query and has no code path that could use anything else.

Screenshots of the imported estate are in `docs/screenshots/reality-pass-*.png`, captured
by the Playwright run rather than staged.

---

## 12. What an external review found after this report was written

Sections 1–11 closed Phase 8. An external review of commit `c16614c` then read the code
this report described and raised **eight findings. All eight held under examination**, and
became the seven fixes [#3](https://github.com/MEGA-M1ND/WorldGraph/pull/3)–[#9](https://github.com/MEGA-M1ND/WorldGraph/pull/9)
— one finding was largely covered by another's fix and completed later by
[#12](https://github.com/MEGA-M1ND/WorldGraph/pull/12).

Six more defects came afterwards, from *running* the application rather than reading it:
five found by driving it in a browser, one by counting the checks on a pull request. All
thirteen are fixed and merged.

They are recorded here because a reality-pass report that omits what it got wrong is not
a reality pass.

### The defects, and what each one had been reported as

The first seven rows are the external review's findings. The last is what driving the CVE
scenario in a browser turned up.

| # | Defect | What §1 had claimed |
|---|---|---|
| [#3](https://github.com/MEGA-M1ND/WorldGraph/pull/3) | Risk scoring read `datetime.now()` internally, so no test could pin an exact score | "Verified" — by tests that could not assert the number they cared about |
| [#4](https://github.com/MEGA-M1ND/WorldGraph/pull/4) | "No vulnerable software found" was returned for assets with **no software inventory at all** | — |
| [#5](https://github.com/MEGA-M1ND/WorldGraph/pull/5) | Entities already degraded before an event were reported as *caused* by it | Blast radius "Verified" |
| [#6](https://github.com/MEGA-M1ND/WorldGraph/pull/6) | A forced re-import silently kept the old estate; the analyst kept answering from it | Import path "Verified" |
| [#7](https://github.com/MEGA-M1ND/WorldGraph/pull/7) | `skip_token` never read — 1 000 of 5 000 resources imported, then HIGH coverage reported over them | "Import scales to 5 000 resources — Verified" |
| [#8](https://github.com/MEGA-M1ND/WorldGraph/pull/8) | Inferred trust hops presented as established fact; every non-internet foothold returned `[]` | — |
| [#9](https://github.com/MEGA-M1ND/WorldGraph/pull/9) | `SECURITY.md` claimed the model "cannot compute"; its prose was returned verbatim with zero tool calls | Tool-boundary claims |
| [#12](https://github.com/MEGA-M1ND/WorldGraph/pull/12) | A vulnerability count and its risk score included assets WorldGraph could see were patched | — |

Plus four layout and interaction defects found in the browser
([#10](https://github.com/MEGA-M1ND/WorldGraph/pull/10),
[#13](https://github.com/MEGA-M1ND/WorldGraph/pull/13),
[#14](https://github.com/MEGA-M1ND/WorldGraph/pull/14),
[#15](https://github.com/MEGA-M1ND/WorldGraph/pull/15)), and one in CI itself
([#11](https://github.com/MEGA-M1ND/WorldGraph/pull/11)): both workflows listened for
`push` on every branch *and* for `pull_request`, so every commit on a branch with an open
PR ran the entire suite twice — nine jobs became eighteen checks, and the browser job was
paid for twice to prove the same thing both times.

### The pattern

**Two of these were the same bug this report claims as its central finding.**

§3 of this document describes absent evidence and negative evidence sharing one
representation — an estate with no data reporting the same thing as an estate that was
searched and found clean. The report presents that as diagnosed and fixed. It was fixed in
the impact model and left in place in the security path (#4) and the blast radius (#5).

So the finding was correct and its application was incomplete, and this report did not
notice the difference. That is worth more than the individual bugs: *knowing* the failure
mode did not prevent shipping two more instances of it.

### What the tests were doing instead

Every defect above lived in the gap between what a test asserted and what a user
experiences:

- The E2E `ask()` helper waited on **message count**, which the app satisfied 0.4 s in. The
  composer stayed disabled for eight more seconds, silently swallowing every keystroke
  (#10).
- **No test covered the vulnerability count or its score at all.** The full suite passed
  unchanged after both were altered (#12).
- The mode-toggle test asserted a **DOM attribute flipped**, which was true while the two
  modes rendered byte-identically (#13).
- The read-only CI check matched **one call site by string**; it now asserts `resources` is
  the only client method invoked anywhere in the adapter (#7).

Four tests written during these fixes were themselves vacuous on first draft — including
one asserting an element was hidden, which also passes when the element does not exist.
Each was caught the same way: revert the fix, run the test, confirm it goes red. That step
is the only thing separating a test from a comment, and it is now how every behavioural
test in these thirteen PRs was accepted.

### What this does not change

**§2 stands exactly as written.** None of this work involved an Azure subscription. The
paging fixed in #7 was validated against a fake client — a test of the loop that was
written, not of Resource Graph. The largest claim in this repository remains unverified,
and thirteen merged fixes have not moved it one step closer to being verified; they have
only made the code around it more honest about what it does not know.

---

## 13. Mutation testing: what the test suite could not tell apart

§12 ends by saying that reverting a fix and watching the test go red "is the only thing
separating a test from a comment". That was true for the thirteen fixes it describes. It
said nothing about the other several hundred tests, which had never been subjected to it.

So they were. Every constant, comparison and boolean operator in the code under audit was
changed one at a time — a `>` to a `<=`, an `and` to an `or`, `1.0` to `2.0`, a string to
that string with an `X` on the end — and the whole suite was run against each. A change
that no test notices is a behaviour no test protects. That is not an opinion about test
quality; it is a decision procedure with a yes or no answer.

The suite went from **597 tests to 1,456** as a result, plus 32 → 39 on the frontend, and
backend line coverage from 92 % to 99 %. Almost none of that is new product code — one line
is, and §13.5 is about that line. The rest is behaviour that was already shipping and could
have been altered silently.

### The five shapes a vacuous assertion takes

Every one of these was written **during this audit, by the same process auditing for
them**, and caught before it was committed. That is the useful part: they are not exotic.

1. **Recomputing the implementation's own expression in the assertion.** A depth test that
   derived the expected depth the same way the code did. It passes for every possible
   implementation, including a wrong one.
2. **Importing the constant under test.** `assert len(paths) == MAX_CRITICAL_PATHS` moves
   with the constant. Replaced by the literal `6`, plus a separate test that the constant
   is 6.
3. **Over-lenient disjunctions.** `assert not found or points == 0` — satisfied by either
   half, so it pins neither.
4. **Inputs below the threshold under test.** A grounding check compared "0.42" against
   `42.0`, but both figures sat under the checker's triviality floor, so the rule being
   tested never ran.
5. **Substring containment on generated prose.** `assert "1 established" in headline`
   passes against `"1 establishedX"`. It can detect deletion and nothing else. Whole-string
   equality or nothing.

### Two blind spots that belong to fixture-based testing itself

These are not careless assertions. They are what happens when one realistic fixture is the
only world a test suite ever sees.

**Saturation.** Three of the four concentration contributions in `material_risk` are pinned
at their ceilings on AtlasPay — the traffic term computes to 73.7 against a cap of 45. Every
multiplier underneath is therefore invisible: change any of them and the output does not
move, because the cap absorbs it. Reaching them needed an estate constructed to sit *below*
every ceiling, which no realistic demo fixture does.

**Overlapping conditions masking each other.** The active-incident filter is severity **and**
correlation. On AtlasPay every severe event correlates, and the one non-correlating event is
`MODERATE` — already excluded by severity. So each half of the condition hid the absence of
the other, and deleting either changed nothing. It took a `LOW` event and an injected
non-correlating `CRITICAL` one to separate them.

### What the survivors were, by module

Ordered by what the finding costs if it goes wrong, not by count.

| Module | What was unprotected |
|---|---|
| `security/sanitize.py` | **All nine prompt-injection patterns.** Each could be broken so it matched nothing; the suite passed on the strength of two example payloads. Also the redaction marker, the NFKC folding path, and every structural cap. |
| `services/world_state.py` | `proximity = 1.0 if internet_facing else 0.0` — the reachability distinction that is the entire point of the security path lived in prose and nowhere in the score. Plus the whole timeline audit trail, including the "none executed" clause that is the product's promise not to touch infrastructure. |
| `services/workspaces.py` | `default_repository_for`, which *is* workspace isolation — one SQLite file per workspace — had no direct test. Two workspaces resolving to the same file would not have been noticed. `_safe_reason`, the only thing between an Azure SDK exception and an API response, was untested. |
| `ai/tools.py` | `MAX_ROWS` and every argument ceiling. These stop one query dumping the estate into a context window and stop an unbounded string reaching the graph lookup and the prompt. |
| `ai/router.py` | The time-window parser (`in the last 2 hours` could resolve to 2 minutes), the CVE and place-name extractors that decide *which* asset is discussed, and the evidence markers — `[INFERRED]` vs `[ESTABLISHED]`, `? (product name only — unverified)`, `MODELLED ESTIMATE`, and the `▲`/`▼` direction arrows. |
| `simulation/engine.py` | `_direction_optional` returning "better" for a missing measurement — painting an undeclared figure green. `_percent` and `_count` substituting a number for `None`. |
| `security/ratelimit.py` | The `max(1, …)` floor, without which a misconfigured limit of 0 locks an endpoint out entirely; per-key isolation; the retry-after figure. |
| `adapters/azure_inventory.py` | Every truncation reason — "Azure said it truncated" and "WorldGraph stopped at its own ceiling" call for opposite responses and could have collapsed onto one string. Every threshold in `level()`, which turns a ratio into a word an operator reads as a verdict. The sanitizer's bounds, and `PAGE_SIZE` — Resource Graph returns at most 1 000 rows whatever KQL asks for, so a larger page size makes the paging loop read a full page as the end of the estate. |
| `geo/spatial.py` | The Earth's radius. The distance tests were there, at `rel=0.01` — ±103 km on a 10 000 km figure, on the number that decides whether an asset sits inside a 50 km exposure radius. Also half the compass table, and the per-category exposure radii whose zeros mean "not geographic at all". |
| `frontend/src/ui/dom.ts` | The `el()` XSS chokepoint, and the codebase-wide property it depends on. The module's own docstring claimed both; nothing tested either. |
| `observability/logging.py` | Log redaction, where the exception branch was not merely unprotected but **wrong** — see §13.5. |
| `storage/repository.py` | Whole methods no test called: `load_events`, `recent_analyses`, `list_scenarios`, `delete_scenario`, `save_plan`, `load_plan`, `load_timeline`, and the transaction rollback. This is what makes a share link work after a restart. |
| `ai/analyst.py` | The entire model-backed tool-use loop and its fallback. It only runs when an API key is configured, which is exactly why it needed testing: code that runs only in a configuration nobody tests fails the first time somebody uses it. |

### What was left alone, and why

Not every survivor is a gap. Three categories were classified rather than closed, because
padding the suite to drive a number to zero is the same failure as writing a vacuous
assertion — it produces a green metric that means nothing.

- **Prose.** Most of the router's survivors are wording inside answer text. Where a phrase
  carries a *claim* — "MODELLED ESTIMATE", "none executed", "[INFERRED]" — it is pinned.
  Where it is only phrasing, it is not, and a test asserting the exact sentence would break
  on every copy edit while protecting nothing.
- **Provably equivalent mutants.** A branch that cannot be reached, or a constant that is
  clamped downstream so the change cannot propagate. These are documented where they were
  examined.
- **Layout, not behaviour.** `@dataclass(slots=True)` and docstrings are filtered by the
  harness before a mutant is generated.

### Pointing it back at its own output

Re-running the harness against the suite the audit had already grown found survivors
inside the audit's own new tests. The clearest: `assert override.id.startswith("ovr-")`
also passes for `"ovr-Xdeadbeef"`, so the id prefix it was written to pin could still
drift. That is vacuous shape 5 again — containment where equality was meant — committed by
the process auditing for it, and caught only because the machine was pointed back at the
work rather than the work being trusted once it was green.

This is the argument for the technique in one line. Careful review had already passed that
assertion twice.

### Where the survivor count actually landed

The harness was run back over its own output at the end: all **666 recorded survivors**,
re-applied one at a time against the grown suite.

| Verdict | Count |
|---|---|
| Now killed by a test written during this audit | **379** |
| No longer exists (the line changed) | 48 |
| Still surviving | **239** |

Of the 239, 130 are in `ai/router.py` and are overwhelmingly prose — wording inside answer
text where no claim is attached. The measurement was taken against a checkout that already
lagged the branch head by several commits, so the true current figure is lower; it is
reported as measured rather than as estimated, because an estimate is exactly the kind of
number this document exists to refuse.

**Zero would be the wrong target.** Driving it there means pinning sentences, and a test
that breaks on every copy edit while protecting nothing is a vacuous assertion wearing a
different hat.

### 13.5 — the one defect this found in shipped code

Everything above is a test gap: behaviour that was correct and unprotected. One thing was
not.

`app/observability/logging.py` opens with **"Secrets never enter a log record"**, and §5 of
this document lists logs as one of three places a credential must never reach.
`JsonFormatter.format` redacted the message and every structured field — and then wrote the
formatted traceback straight into the payload without passing it through `_redact`.

Both `logger.exception` call sites in the codebase are precisely where a credential-bearing
exception arrives: the unhandled-request handler, and `ai_tool_failed`. An HTTP error
carrying a key in its URL, or a cloud SDK error carrying a bearer fragment, reached the log
verbatim.

It was found by *covering* the module, not by reading it. Five of its branches had never
been executed and the exception branch was one of them. Nothing in the mutation audit would
have found it either — a mutant cannot be killed on a line no test runs.

That is the argument for measuring coverage alongside mutation score, and it is why the
second half of this audit switched technique. A surviving mutant means "no test can tell the
difference". An uncovered line means "no test runs this", which is strictly worse and was
strictly more productive to chase: `storage/repository.py` (76 %), `ai/tools.py` (77 %),
`ai/analyst.py` (54 %), `adapters/base.py` (83 %) and `api/routes.py` (91 %) were all whole
methods and error paths that no test called — persistence, the tool failure surface, the
model-backed analyst, feed error sanitisation, and every HTTP 404 and 422 the frontend codes
against.

### 13.6 — a second finding: an explanation that can never fire

Chasing the last uncovered lines in `analysis/blast_radius.py` turned up dead code rather
than a missing test.

`_explain` builds a line reading *"X's largest single dependency loss comes from Y"* by
looking up each origin in the propagation state's `dominant_cause` map. It never fires.
`calculate_blast_radius` pins every origin, a pinned entity is a boundary condition the
solver deliberately refuses to attribute a cause to — *"otherwise 'Singapore is DOWN' would
quietly become 'mostly up'"* — and `_explain` has exactly one caller. So the branch is
unreachable, and `dominant_cause` is computed by the propagation engine and consumed
nowhere.

This is not a wrong answer, it is a missing one: an explanation somebody wrote and nobody
has ever seen. It is recorded rather than fixed, because making it fire is a change to what
every analysis says, and that is a product decision rather than a test one.

The invariant underneath it — a pinned origin cannot be healed by its own dependencies — is
real and is now pinned by a test. The dead branch is left as it is, named here.

### What the two techniques cost, and which paid

Roughly in proportion: the mutation pass took most of the wall clock — a full run is
~1,100 mutants, each one a complete suite execution — and produced the sharper individual
findings, the ones about a specific constant carrying a specific claim. The coverage pass
took minutes and produced more of them, because "no test runs this line" is a cheaper
question to answer than "can any test tell this apart", and on a suite this size it was
still true of about eight per cent of the code.

Both were needed. Coverage would never have found the reachability factor in CVE scoring —
that line runs on every security analysis, it just runs unchecked. Mutation would never have
found the log-redaction defect, because a mutant cannot be killed on a line no test runs.

### The honest limit of this

Mutation testing proves a test *can* fail. It does not prove the behaviour is correct — a
wrong constant pinned by a test is still wrong, now with a test defending it. Four of the
new tests failed on first run against the real implementation and were corrected: a
tri-state polarity inverted, two comparison keys guessed rather than measured, and a
scenario builder called with the wrong keyword. Each of those was the test being wrong, not
the code. That is the failure mode this technique replaces the old one with, and it is a
better one to have, but it is not nothing.

**And it changes nothing about §2.** Every mutant in this audit ran against fixtures and
fakes. Not one of them ran against an Azure subscription.
