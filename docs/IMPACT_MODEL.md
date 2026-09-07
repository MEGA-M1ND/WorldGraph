# WorldGraph V1 Impact Model

Every number WorldGraph shows comes from this document. It is deliberately simple,
completely deterministic, and named — *the WorldGraph V1 Impact Model* — everywhere it
surfaces, so nobody mistakes it for a general reliability model.

> **All outputs are MODELLED ESTIMATES over synthetic data.** WorldGraph has no access to
> AtlasPay's ledger, its telemetry, or its contracts. It has a graph and arithmetic, and it
> says so in the schema (`BusinessImpact.disclaimer` is a literal type) so a careless
> serializer cannot drop the label.

---

## 1. Health and availability

Each entity has a discrete `HealthState` mapped to an availability multiplier in `[0, 1]`:

| State | Value | Reading |
|---|---:|---|
| `HEALTHY` | 1.0 | serving normally |
| `DEGRADED` | 0.7 | serving, impaired |
| `SEVERELY_DEGRADED` | 0.3 | mostly failing |
| `DOWN` | 0.0 | not serving |
| `UNKNOWN` | 0.9 | **no telemetry** |

`UNKNOWN = 0.9` is a deliberate middle: treating unknown as healthy hides risk, treating it
as down invents outages. The 10 % haircut makes missing telemetry visible in the numbers
without fabricating an incident.

Converting back (`health_from_value`) uses the midpoints between anchors, so
`health_from_value(HEALTH_VALUES[h]) == h` holds for every real state.

*Implementation:* `backend/app/models/core.py`. *Tests:* `test_impact_model.py::TestHealthRoundTrip`.

---

## 2. Failure propagation

For an entity `X` with dependencies `T₁…Tₙ`:

```
dependencyAvailability(X) = Π  [ 1 − criticalityᵢ · (1 − availability(Tᵢ)) · (1 − redundancyᵢ) ]
                            ᵢ

effectiveAvailability(X)  = baseAvailability(X) · dependencyAvailability(X)
```

Reading the term for one dependency: a target that is fully down removes `criticality` of
`X`'s function, and `redundancy` of *that loss* is absorbed by failover.

| criticality | redundancy | target down → multiplier |
|---:|---:|---:|
| 1.0 | 0.0 | **0.00** — hard dependency, takes X down with it |
| 1.0 | 0.55 | 0.55 — warm peer carries most of the load |
| 0.45 | 0.4 | 0.73 — a feature degrades, the service survives |
| any | 1.0 | 1.00 — fully redundant; this edge alone cannot hurt X |

### What counts as a dependency

| Edge | Direction of failure | In the model? |
|---|---|---|
| `DEPENDS_ON`, `HOSTED_IN`, `CONNECTS_TO`, `SUPPLIED_BY` | target → source | ✅ outgoing |
| `SERVES` | source → target (provider to consumer) | ✅ incoming, for customer regions |
| `REPLICATES_TO` | — | ❌ **excluded.** A replica failing does not take its primary down; that asymmetry is the entire reason replicas exist. |

*Implementation:* `analysis/propagation.py::resolve_inputs`.

### Solving cycles

Real estates contain dependency cycles. WorldGraph solves by **Jacobi iteration**: every
round recomputes each entity from the previous round's values. Availability is monotone
non-increasing and bounded below by 0, so the iteration always converges; it stops early
once nothing moves by more than `1e-4`, and reports `converged = False` if it ever hits the
64-round cap.

Traversal separately detects and records cycles rather than following them, because
multiplying availability around a loop would drive everything to zero.

---

## 3. Capacity — and why it is solved separately

This is the model's one genuinely load-bearing idea.

```
capacity(X) = baseCapacity(X) · Π [ 1 − capacityImpactᵢ · (1 − capacity(Tᵢ)) · (1 − redundancyᵢ) ]
                                ᵢ
```

Same shape, different coupling (`DependencyEdge.capacity_impact`, defaulting to
`criticality`) and solved from the dependency's *capacity* rather than its availability.

**Availability answers "are requests succeeding right now". Capacity answers "how much load
could we take".** For most edges they coincide. For supply chains they must not:

> Losing a sole-source hardware supplier does not switch a running Kubernetes cluster off
> today. It removes the ability to replace failed nodes and to scale. Reporting one number
> for both is how a model turns "constrained replacement capacity" into a fictional outage.

AtlasPay's `payments-k8s-singapore ⟶ supplier-taiwan-hardware` edge therefore carries
`criticality = 0.12` and `capacity_impact = 0.65`. With the supplier down:

| | Value |
|---|---:|
| Cluster availability | **89 %** — traffic still flowing |
| Cluster capacity | **42 %** — cannot replace or grow |
| APAC regional capacity | **69 %** |
| Organisation availability | **90.5 %** |

Capacity is also a **ceiling on availability**: a cluster that can serve 40 % of its load
cannot answer more than 40 % of its requests.

*Tests:* `test_impact_model.py::test_capacity_and_availability_diverge_for_supply_chains`.

---

## 4. Geospatial exposure

### Earthquake radius

```
radius_km = 10 · 10^(0.4·(M − 4))        clamped to [10, 900]
deep events (depth > 70 km) × 0.75
```

| Magnitude | Radius |
|---:|---:|
| M4.0 | 10 km |
| M5.0 | 25 km |
| M6.0 | 63 km |
| M6.8 | 132 km |
| M7.5 | 251 km |
| M8.0 | 398 km |

This is **not** a ground motion prediction equation. A real GMPE needs site conditions and
a regional attenuation model that a synthetic estate cannot supply. What this model
guarantees is the property the correlation and risk layers actually rely on: monotonicity in
magnitude, at the right order of magnitude for *infrastructure disruption*.

The `0.4` exponent is a judgement call. `0.5` produces radii roughly twice as wide, which
sweeps in assets no operator would accept as exposed.

Other categories use coarse per-category baselines (`geo/spatial.py`). Cloud incidents,
service incidents and vulnerabilities have **radius 0** — they are not geographic events and
correlate by named region or software inventory instead.

### Exposure strength

```
proximity = 1 − distance / radius          (0 at the edge, 1 at the epicentre)
```

Linear, not inverse-square: the radius already encodes the attenuation, and stacking two
decay models would double-count and make everything look safe.

### Modelled damage

```
availability = 1 − proximity · (1 − severityFloor)
```

| Event severity | Floor at the epicentre |
|---|---:|
| CRITICAL | 0.10 |
| HIGH | 0.30 |
| MODERATE | 0.60 |
| LOW | 0.85 |
| INFO | 0.97 |

The floor is never 0. A facility inside a severe earthquake radius is disrupted, not
vaporised, and claiming certain destruction from a magnitude number alone would be
dishonest.

**Only physical entities correlate geographically.** A microservice is not "near" an
earthquake in any useful sense — it inherits impact through the site that hosts it, which is
the graph's job, not geography's.

---

## 5. Risk score

A 0-100 score that is always the sum of named, signed contributions.

| Code | Max | Scaled by |
|---|---:|---|
| `asset_criticality` | +30 | criticality weight × **availability actually lost** |
| `customer_facing` | +25 | worst customer-facing availability loss × 1.6 |
| `single_point_of_failure` | +20 | worst unredundant entity's loss |
| `customer_exposure` | +20 | traffic impact × 2.2 |
| `event_proximity` | +15 | proximity factor |
| `event_severity` | +15 | event's own severity band |
| `dependency_depth` | +10 | hops travelled, saturating at 5 |
| `redundancy_credit` | **−12** | origin has ≥2 independent replicas |
| `freshness_penalty` | **−8** | observation older than 1 h, full at 12 h |

Two properties worth stating:

* **Every term scales with actual loss, not mere presence.** A CRITICAL asset that lost 5 %
  of its availability is not the same event as one that is gone. A flat "a critical thing
  was touched" term makes every incident look like a catastrophe, which is how a risk score
  stops carrying information.
* **Two terms are negative.** A resilient estate must be able to score *lower* for the same
  event, and a stale observation must not carry a full-confidence score. A model that can
  only add points is a model that only ever escalates.

No single dimension can reach CRITICAL alone: 75 points needs at least three of them.

### Bands

| Score | Severity |
|---|---|
| 0 – 24.99 | LOW |
| 25 – 49.99 | MODERATE |
| 50 – 74.99 | HIGH |
| 75 – 100 | CRITICAL |

Boundaries are pinned by `test_impact_model.py::TestRiskBands`.

### Worked example — the hero scenario

M6.8 near Hsinchu, 20 km from AtlasPay's sole-source supplier (radius 132 km, proximity 0.85,
supplier modelled at 40 % availability):

```
HIGH (68.7/100)
  +18  critical asset impacted        Taiwan Hardware Supplier at 40% availability
   +3  customer-facing service degraded
  +12  single-region dependency with no failover
  +13  event proximity to enterprise assets
  +11  high severity event
   +2  customer traffic exposure
  +10  deep dependency propagation
```

**Scored at the instant the scenario depicts.** The score also carries a freshness term:
an observation more than an hour old loses up to 8 points, reaching the full penalty at
twelve hours. That is deliberate — a day-old reading should not carry a fresh reading's
authority — but it makes any exact score a statement about *when* it was evaluated. The
engine therefore takes the clock as an argument (`calculate_blast_radius(..., now=...)`)
rather than reading it from the environment, and the regression tests pass
`REPLAY_EVALUATED_AT`. Replaying this fixture a day later scores it 60.7, correctly.

Escalating it in simulation to *supplier DOWN + Singapore cluster DOWN* takes the same
formula to **CRITICAL (90/100)**.

---

## 6. Business impact

| Figure | Derivation |
|---|---|
| Organisation availability | traffic-weighted availability across **customer regions** — what users experience, not how many boxes are green |
| Traffic impact | `1 − availability` |
| Customers affected | sum of `customer_count` for regions below the impact threshold |
| Revenue at risk / hour | `Σ revenue_per_hour × (1 − availability)` per region |
| SLA breaches | entities below their tier's floor (TIER-0 99.95 %, TIER-1 99.9 %, TIER-2 99 %) |
| Regional capacity | traffic-weighted **capacity** of clusters and microservices in that region |

Regional capacity deliberately excludes cloud regions and databases: a provider's region is
not AtlasPay's capacity, and counting it dilutes the figure with infrastructure the company
neither owns nor scales.

**Impact threshold: 0.999.** Below it an entity is "impacted"; above it the drop is noise
from a partially-redundant dependency and would clutter every result.

---

## 7. Confidence

Every analysis carries a confidence score *and the reasons for it*. The score starts from
the data source's own confidence and adjusts for things WorldGraph can actually check:

| Adjustment | Effect |
|---|---:|
| Event reported live by a named source | evidence |
| Facility inside the modelled event radius | +0.10 |
| Explicit dependency edges exist | +0.08 |
| Observation older than 6 hours | −0.10 |
| Traversal was truncated | −0.15 |
| Propagation did not converge | −0.10 |
| Origin is a supplier (operating status unavailable) | −0.05 |
| Origin has no health telemetry | −0.05 |

Uncertainties are named explicitly rather than folded into a number, and the list always
ends with *"enterprise estate is synthetic demo data (AtlasPay)"* — a fact no analysis is
allowed to omit.

---

## 8. Assumptions and limitations

WorldGraph V1 does **not** model:

- queueing, retry storms, or saturation dynamics — failure is instantaneous and first-order;
- correlated failure beyond declared edges (a shared power feed nothing declares is invisible);
- time. There is no MTTR, no recovery curve, no lead-time simulation — capacity impact is
  reported as a *state*, not a schedule;
- partial traffic shifting. The model says a peer *could* absorb load; it does not simulate
  the shift;
- CVE version ranges. Matching is by explicit CVE id or product name. WorldGraph is not a
  scanner, and range logic over a synthetic inventory would be theatre;
- exploitability. Attack paths are **declared network adjacency**. WorldGraph does not test
  authentication, network policy, or whether an exploit works.

It also inherits the honesty constraints of its inputs: dependency criticality and
redundancy are modelled values, not measurements, and the entire AtlasPay estate is
invented.

---

## 9. Where to look

| Concern | File |
|---|---|
| Health values, bands, edge semantics | `backend/app/models/core.py` |
| Propagation solver | `backend/app/analysis/propagation.py` |
| Risk scoring and confidence | `backend/app/analysis/risk.py` |
| Business impact and regional capacity | `backend/app/analysis/business_impact.py` |
| Exposure radius and proximity | `backend/app/geo/spatial.py` |
| Correlation | `backend/app/analysis/correlation.py` |
| Blast radius assembly | `backend/app/analysis/blast_radius.py` |
| Tests pinning all of it | `backend/tests/test_impact_model.py`, `test_correlation.py` |
