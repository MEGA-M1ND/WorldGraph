"""Geospatial correlation, exposure radius, and security correlation."""

from __future__ import annotations

import pytest

from app.adapters.fixtures import demo_vulnerability, singapore_region_outage, taiwan_earthquake
from app.analysis.correlation import (
    attack_paths,
    correlate_event,
    find_assets_near_event,
    match_vulnerable_assets,
    modelled_availability,
    proximity_summary,
)
from app.geo.spatial import (
    bearing_degrees,
    compass_point,
    earthquake_exposure_radius_km,
    exposure_radius_for,
    haversine_km,
    proximity_factor,
    within_radius,
)
from app.graph.world_graph import WorldGraph
from app.models.core import (
    DataMode,
    DataSourceInfo,
    DependencyEdge,
    DependencyType,
    EntityType,
    EventCategory,
    GeoPoint,
    Severity,
    WorldEntity,
)

SINGAPORE = GeoPoint(lat=1.3521, lon=103.8198)
FRANKFURT = GeoPoint(lat=50.1109, lon=8.6821)
HSINCHU = GeoPoint(lat=24.7736, lon=120.9417)


class TestDistance:
    def test_identical_points_are_zero_apart(self):
        assert haversine_km(SINGAPORE, SINGAPORE) == pytest.approx(0.0, abs=1e-9)

    def test_known_distance(self):
        """Singapore → Frankfurt is ~10,258 km great-circle.

        `rel=0.01` used to be the tolerance here, which is ±103 km — wide enough that the
        Earth radius constant itself could drift by a kilometre with nothing failing, on a
        figure that decides whether an asset sits inside a 50 km exposure radius.
        Mutation testing found exactly that. 0.05 % is float noise; 1 % is a different
        planet.
        """
        assert haversine_km(SINGAPORE, FRANKFURT) == pytest.approx(10_258.05, rel=5e-4)

    def test_the_earth_radius_is_the_iugg_mean(self):
        """6371.0088 km. Written out rather than imported, or it would pin itself."""
        from app.geo.spatial import EARTH_RADIUS_KM

        assert EARTH_RADIUS_KM == 6371.0088

    def test_a_degree_of_latitude_is_a_hundred_and_eleven_kilometres(self):
        """The schoolbook figure, and an independent check on the radius."""
        assert haversine_km(
            GeoPoint(lat=0.0, lon=0.0), GeoPoint(lat=1.0, lon=0.0)
        ) == pytest.approx(111.195, rel=5e-4)

    def test_the_equator_to_the_pole_is_a_quarter_of_the_circumference(self):
        assert haversine_km(
            GeoPoint(lat=0.0, lon=0.0), GeoPoint(lat=90.0, lon=0.0)
        ) == pytest.approx(10_007.56, rel=5e-4)

    def test_symmetry(self):
        assert haversine_km(SINGAPORE, FRANKFURT) == pytest.approx(
            haversine_km(FRANKFURT, SINGAPORE)
        )

    def test_antipodal_does_not_overflow(self):
        """The sqrt clamp exists for exactly this input."""
        north = GeoPoint(lat=90.0, lon=0.0)
        south = GeoPoint(lat=-90.0, lon=0.0)
        assert haversine_km(north, south) == pytest.approx(20_015.11, rel=5e-4)

    def test_dateline_is_a_short_hop_not_a_lap(self):
        west = GeoPoint(lat=0.0, lon=179.9)
        east = GeoPoint(lat=0.0, lon=-179.9)
        assert haversine_km(west, east) < 30

    def test_within_radius_is_inclusive(self):
        assert within_radius(SINGAPORE, SINGAPORE, 1.0) is True

    def test_zero_radius_matches_nothing(self):
        assert within_radius(SINGAPORE, SINGAPORE, 0.0) is False


class TestBearing:
    def test_due_north(self):
        origin = GeoPoint(lat=0.0, lon=0.0)
        assert bearing_degrees(origin, GeoPoint(lat=10.0, lon=0.0)) == pytest.approx(0.0, abs=0.1)

    def test_due_east(self):
        origin = GeoPoint(lat=0.0, lon=0.0)
        assert bearing_degrees(origin, GeoPoint(lat=0.0, lon=10.0)) == pytest.approx(90.0, abs=0.1)

    def test_due_south_and_west(self):
        origin = GeoPoint(lat=0.0, lon=0.0)
        assert bearing_degrees(origin, GeoPoint(lat=-10.0, lon=0.0)) == pytest.approx(180.0, abs=0.1)
        assert bearing_degrees(origin, GeoPoint(lat=0.0, lon=-10.0)) == pytest.approx(270.0, abs=0.1)

    @pytest.mark.parametrize(
        ("bearing", "expected"),
        [
            # All eight labels. Only four were tested; SE, SW and NW were unreachable by
            # any assertion, so the table could have been reordered — and "42 km SW of the
            # epicentre" is a factual claim in an answer, not decoration.
            (0, "N"), (45, "NE"), (90, "E"), (135, "SE"),
            (180, "S"), (225, "SW"), (270, "W"), (315, "NW"),
            # Each sector's own boundaries: the label changes 22.5° either side of centre.
            (22.4, "N"), (22.5, "NE"), (67.4, "NE"), (67.5, "E"),
            (337.4, "NW"), (337.5, "N"), (359, "N"),
            # Angles outside 0-360 wrap rather than falling off the end of the table.
            (360, "N"), (405, "NE"), (-45, "NW"), (-90, "W"), (720, "N"),
        ],
    )
    def test_compass_labels(self, bearing: float, expected: str):
        assert compass_point(bearing) == expected

    def test_the_eight_labels_are_eight_distinct_labels(self):
        """A duplicated entry would silently merge two sectors."""
        labels = [compass_point(b) for b in range(0, 360, 45)]
        assert len(set(labels)) == 8


class TestExposureRadius:
    def test_radius_is_monotonic_in_magnitude(self):
        radii = [earthquake_exposure_radius_km(m) for m in (4.0, 5.0, 6.0, 7.0, 8.0)]
        assert radii == sorted(radii)

    @pytest.mark.parametrize(
        ("magnitude", "expected_km"),
        [(4.0, 10.0), (5.0, 25.1), (6.0, 63.1), (6.8, 131.8), (7.5, 251.2)],
    )
    def test_known_magnitudes(self, magnitude: float, expected_km: float):
        """Pins the documented worked examples in docs/IMPACT_MODEL.md."""
        assert earthquake_exposure_radius_km(magnitude) == pytest.approx(expected_km, rel=0.01)

    def test_deep_events_have_a_smaller_damage_footprint(self):
        shallow = earthquake_exposure_radius_km(6.5, depth_km=10.0)
        deep = earthquake_exposure_radius_km(6.5, depth_km=300.0)
        assert deep < shallow

    def test_radius_is_clamped(self):
        assert earthquake_exposure_radius_km(0.0) == 10.0
        assert earthquake_exposure_radius_km(12.0) == 900.0

    def test_non_geographic_categories_have_no_radius(self):
        assert exposure_radius_for(EventCategory.SECURITY_VULNERABILITY) == 0.0
        assert exposure_radius_for(EventCategory.CLOUD_INCIDENT) == 0.0
        assert exposure_radius_for(EventCategory.SERVICE_INCIDENT) == 0.0

    @pytest.mark.parametrize(
        ("category", "expected_km"),
        [
            (EventCategory.WILDFIRE, 30.0),
            (EventCategory.SEVERE_WEATHER, 150.0),
            (EventCategory.FLOOD, 60.0),
            (EventCategory.POWER_OUTAGE, 50.0),
            (EventCategory.NETWORK_OUTAGE, 250.0),
            (EventCategory.SUPPLY_CHAIN, 200.0),
            (EventCategory.OTHER, 50.0),
            # Zero means "not geographic at all" — these correlate by named region or by
            # software inventory. A non-zero here would make a cloud status page start
            # matching assets by distance, which is precisely the §21 confusion.
            (EventCategory.CLOUD_INCIDENT, 0.0),
            (EventCategory.SERVICE_INCIDENT, 0.0),
            (EventCategory.SECURITY_VULNERABILITY, 0.0),
        ],
    )
    def test_the_category_baselines_are_these_figures(self, category, expected_km):
        """The fallback footprint decides which assets correlate at all.

        Written out as literals rather than read from `_CATEGORY_BASE_RADIUS_KM`, which
        would assert the table against itself.
        """
        assert exposure_radius_for(category) == expected_km

    def test_every_category_has_a_declared_radius(self):
        """A new category must not silently inherit the 50 km `.get` default."""
        from app.geo.spatial import _CATEGORY_BASE_RADIUS_KM

        assert set(_CATEGORY_BASE_RADIUS_KM) == set(EventCategory)

    def test_an_earthquake_ignores_the_category_baseline(self):
        """It has its own model; the 100 km entry is a fallback that must never be used."""
        assert exposure_radius_for(
            EventCategory.EARTHQUAKE, {"magnitude": 6.0}
        ) == pytest.approx(63.1, rel=0.01)

    def test_a_negative_explicit_radius_is_floored_at_zero(self):
        assert exposure_radius_for(EventCategory.WILDFIRE, {"radius_km": -5.0}) == 0.0

    def test_explicit_radius_metadata_wins(self):
        assert exposure_radius_for(EventCategory.WILDFIRE, {"radius_km": 12.5}) == 12.5

    def test_malformed_metadata_falls_back_rather_than_raising(self):
        radius = exposure_radius_for(EventCategory.EARTHQUAKE, {"magnitude": "not-a-number"})
        assert radius == pytest.approx(earthquake_exposure_radius_km(4.0))


class TestProximityFactor:
    def test_epicentre_is_full_exposure(self):
        assert proximity_factor(0.0, 100.0) == 1.0

    def test_edge_is_zero(self):
        assert proximity_factor(100.0, 100.0) == 0.0

    def test_beyond_the_edge_is_zero(self):
        assert proximity_factor(500.0, 100.0) == 0.0

    def test_halfway(self):
        assert proximity_factor(50.0, 100.0) == pytest.approx(0.5)

    def test_zero_radius_exposes_nothing(self):
        assert proximity_factor(0.0, 0.0) == 0.0


class TestSpatialCorrelation:
    def test_taiwan_quake_hits_the_taiwan_assets(self, atlaspay_graph: WorldGraph):
        matches = find_assets_near_event(atlaspay_graph, taiwan_earthquake())
        found = {m.entity.id for m in matches}
        assert "supplier-taiwan-hardware" in found
        assert "dc-taiwan-hsinchu" in found
        assert "factory-taiwan-assembly" in found

    def test_taiwan_quake_does_not_reach_frankfurt(self, atlaspay_graph: WorldGraph):
        found = {m.entity.id for m in find_assets_near_event(atlaspay_graph, taiwan_earthquake())}
        assert "cloud-region-frankfurt" not in found
        assert "office-frankfurt" not in found

    def test_matches_are_ordered_by_distance(self, atlaspay_graph: WorldGraph):
        matches = find_assets_near_event(atlaspay_graph, taiwan_earthquake())
        assert [m.distance_km for m in matches] == sorted(m.distance_km for m in matches)

    def test_logical_entities_are_not_geographically_exposed(self, atlaspay_graph: WorldGraph):
        """A microservice is not 'near' an earthquake — it inherits impact via its host."""
        found = {m.entity.id for m in find_assets_near_event(atlaspay_graph, taiwan_earthquake())}
        assert "payments-api" not in found
        assert "checkout-platform" not in found

    def test_events_without_a_location_match_nothing(self, atlaspay_graph: WorldGraph):
        assert find_assets_near_event(atlaspay_graph, demo_vulnerability()) == []

    def test_zero_radius_events_match_nothing(self, atlaspay_graph: WorldGraph):
        """A named-region provider incident has coordinates but no radius."""
        assert find_assets_near_event(atlaspay_graph, singapore_region_outage()) == []

    def test_named_entities_correlate_without_geometry(self, atlaspay_graph: WorldGraph):
        pinned, matches, proximity = correlate_event(atlaspay_graph, singapore_region_outage())
        assert matches == []
        assert "cloud-region-singapore" in pinned
        assert proximity == 1.0

    def test_modelled_availability_falls_off_with_distance(self):
        event = taiwan_earthquake()
        assert modelled_availability(event, 1.0) < modelled_availability(event, 0.2)
        assert modelled_availability(event, 0.0) == 1.0

    def test_severity_floor_is_never_total_destruction(self):
        """A magnitude number alone cannot justify claiming a facility is gone."""
        assert modelled_availability(taiwan_earthquake(), 1.0) > 0.0

    def test_proximity_summary_counts_facilities_and_suppliers(self, atlaspay_graph: WorldGraph):
        summary = proximity_summary(atlaspay_graph, taiwan_earthquake())
        assert summary["suppliers"] >= 1
        assert summary["critical_facilities"] >= 1
        assert summary["dependent_services"] >= 1


class TestSecurityCorrelation:
    def test_cve_matches_the_assets_running_the_package(self, atlaspay_graph: WorldGraph):
        matches = match_vulnerable_assets(atlaspay_graph, cve_id="CVE-2026-DEMO-001")
        found = {m.entity.id for m in matches}
        assert {"admin-api", "payments-api", "checkout-worker"} <= found

    def test_patched_assets_are_not_matched(self, atlaspay_graph: WorldGraph):
        found = {
            m.entity.id
            for m in match_vulnerable_assets(atlaspay_graph, cve_id="CVE-2026-DEMO-001")
        }
        assert "portal-k8s-frankfurt" not in found, "3.5.0 is patched"
        assert "payments-k8s-mumbai" not in found

    def test_matching_is_case_insensitive(self, atlaspay_graph: WorldGraph):
        assert match_vulnerable_assets(atlaspay_graph, cve_id="cve-2026-demo-001")

    def test_product_name_matching(self, atlaspay_graph: WorldGraph):
        assert match_vulnerable_assets(atlaspay_graph, product_names=["postgresql"])

    def test_unknown_cve_matches_nothing(self, atlaspay_graph: WorldGraph):
        assert match_vulnerable_assets(atlaspay_graph, cve_id="CVE-1999-0001") == []

    def test_internet_facing_assets_sort_first(self, atlaspay_graph: WorldGraph):
        matches = match_vulnerable_assets(atlaspay_graph, cve_id="CVE-2026-DEMO-001")
        assert matches[0].internet_facing is True

    def test_attack_path_reaches_payments_through_trust(self, atlaspay_graph: WorldGraph):
        """internet → admin-api → internal-auth → payments-api.

        The last hop moves *against* the dependency arrow: payments-api trusts
        internal-auth. An egress-only walk would report this path as nonexistent.
        """
        paths = attack_paths(atlaspay_graph, to_entity_ids=["payments-api"])
        assert paths, "the payments path must be reachable"
        route = next(p for p in paths if "internal-auth" in p.nodes)
        assert route.nodes[0] == "internet"
        assert route.nodes[1] == "admin-api"
        assert route.nodes[-1] == "payments-api"

        # The trust hop is kept, because an auth-service pivot is real — and marked, because
        # an operational dependency edge cannot distinguish that from a shared database.
        assert route.relies_on_inference is True
        assert route.basis == "INFERRED"
        inferred = [hop for hop in route.hops if hop.is_inferred]
        assert inferred and inferred[0].to_entity_id == "payments-api"
        assert "trust relationship" in inferred[0].evidence
        # A path is worth its weakest hop, not its average.
        assert route.confidence == min(hop.confidence for hop in route.hops)

    def test_attack_paths_are_deterministic(self, atlaspay_graph: WorldGraph):
        first = attack_paths(atlaspay_graph, to_entity_ids=["payments-api"])
        second = attack_paths(atlaspay_graph, to_entity_ids=["payments-api"])
        assert first == second

    def test_supply_and_customer_edges_are_not_attack_surface(self, atlaspay_graph: WorldGraph):
        """A supply contract is not network adjacency."""
        paths = attack_paths(atlaspay_graph, to_entity_ids=["supplier-taiwan-hardware"])
        assert paths == []

    def test_unknown_origin_yields_no_paths(self, atlaspay_graph: WorldGraph):
        assert attack_paths(atlaspay_graph, from_entity_id="nowhere") == []


class TestSeverityFloorsArePinned:
    """The floors themselves, not just that they are ordered.

    Found by mutation testing: changing `_SEVERITY_FLOOR[CRITICAL]` from 0.10 to 1.10 —
    which says a facility at the centre of a CRITICAL event is completely unaffected —
    failed no test. Four of the five floors survived the same treatment. Only HIGH was
    pinned, and only because the AtlasPay fixture happens to be a HIGH event.

    Three assertions covered this table: that availability falls off with distance, that it
    is 1.0 at zero proximity, and that it is greater than zero at the centre. All three are
    relative or one-sided, and none of them names a number.

    These floors are a deliberate modelling decision — the comment above the table argues
    that a magnitude number alone cannot justify claiming a facility is gone — so they are
    exactly the kind of value that should not be able to drift unnoticed.
    """

    @pytest.mark.parametrize(
        ("severity", "floor"),
        [
            (Severity.CRITICAL, 0.10),
            (Severity.HIGH, 0.30),
            (Severity.MODERATE, 0.60),
            (Severity.LOW, 0.85),
            (Severity.INFO, 0.97),
        ],
    )
    def test_each_floor_is_the_documented_value(self, severity: Severity, floor: float):
        """At full proximity an asset retains exactly the floor for its severity."""
        event = demo_vulnerability().model_copy(update={"severity": severity})
        assert modelled_availability(event, 1.0) == pytest.approx(floor)

    def test_every_severity_is_covered(self):
        """A new severity must not silently inherit someone else's floor."""
        for severity in Severity:
            event = demo_vulnerability().model_copy(update={"severity": severity})
            assert 0.0 < modelled_availability(event, 1.0) <= 1.0

    def test_the_floors_are_ordered_by_severity(self):
        floors = [
            modelled_availability(
                demo_vulnerability().model_copy(update={"severity": s}), 1.0
            )
            for s in (Severity.CRITICAL, Severity.HIGH, Severity.MODERATE, Severity.LOW, Severity.INFO)
        ]
        assert floors == sorted(floors), "a worse event must not leave more availability"

    def test_no_severity_models_total_destruction(self):
        """The property the original test asserted, now for every severity, not just HIGH."""
        for severity in Severity:
            event = demo_vulnerability().model_copy(update={"severity": severity})
            assert modelled_availability(event, 1.0) > 0.0


class TestProximitySummaryIsPinned:
    """The numbers on the event card, not just that they are at least one.

    Found by mutation testing. `test_proximity_summary_counts_facilities_and_suppliers`
    asserted `>= 1` for each count, which survives almost any error: flipping
    `== "SUPPLIER"` to `!=`, starting `dependent` at 1 instead of 0, changing the traversal
    depth from 4 to 5, or dropping any single entity type from either classification set
    all left the suite green.

    These four numbers are the "Enterprise proximity" panel an operator reads first when an
    event is selected. They should not be able to drift.
    """

    def test_the_taiwan_quake_summary_is_exactly_this(self, atlaspay_graph: WorldGraph):
        assert proximity_summary(atlaspay_graph, taiwan_earthquake()) == {
            "critical_facilities": 2,   # dc-taiwan-hsinchu, factory-taiwan-assembly
            "suppliers": 1,             # supplier-taiwan-hardware
            "dependent_services": 8,
            "assets_in_radius": 3,
        }

    def test_facilities_and_suppliers_partition_the_matches(self, atlaspay_graph: WorldGraph):
        """A supplier is not a facility, and together they account for everything matched.

        This is what makes the two counts independent: if `== "SUPPLIER"` were inverted,
        or a type moved between the sets, the parts would stop summing to the whole.
        """
        summary = proximity_summary(atlaspay_graph, taiwan_earthquake())
        assert summary["critical_facilities"] + summary["suppliers"] == summary["assets_in_radius"]

    def test_every_matched_entity_is_classified(self, atlaspay_graph: WorldGraph):
        """No matched asset falls through both sets and is silently uncounted."""
        matches = find_assets_near_event(atlaspay_graph, taiwan_earthquake())
        assert matches, "the fixture must match something or this proves nothing"
        facility_types = {"DATACENTER", "OFFICE", "CLOUD_REGION", "FACTORY", "NETWORK_NODE"}
        for match in matches:
            kind = match.entity.type.value
            assert kind in facility_types or kind == "SUPPLIER", f"{kind} is counted by nothing"

    def test_the_traversal_depth_bound_is_observable(self):
        """A chain longer than the bound is cut at the bound, and the number says so.

        Built rather than reimplemented. My first attempt at this test recomputed the
        count with the same expression the code uses, so it moved in lockstep with the
        implementation and could not detect a change in it — the very fault this audit is
        about. A constructed graph with a known answer has no such coupling.
        """
        source = DataSourceInfo(source_id="t", source_name="t", mode=DataMode.SYNTHETIC)
        entities = [
            WorldEntity(
                id="dc",
                name="dc",
                type=EntityType.DATACENTER,
                source=source,
                location=HSINCHU,
            )
        ]
        edges = []
        previous = "dc"
        for depth in range(1, 7):  # six hops, two beyond the bound of four
            entities.append(
                WorldEntity(
                    id=f"svc{depth}",
                    name=f"svc{depth}",
                    type=EntityType.MICROSERVICE,
                    source=source,
                )
            )
            edges.append(
                DependencyEdge(
                    id=f"e{depth}",
                    source_entity_id=f"svc{depth}",
                    target_entity_id=previous,
                    type=DependencyType.DEPENDS_ON,
                )
            )
            previous = f"svc{depth}"

        summary = proximity_summary(WorldGraph(entities, edges), taiwan_earthquake())
        assert summary["dependent_services"] == 4, "the depth-4 bound must be what stops the walk"
        assert summary["critical_facilities"] == 1
        assert summary["assets_in_radius"] == 1

    def test_the_depth_guard_is_redundant_given_physical_only_matching(
        self, atlaspay_graph: WorldGraph
    ):
        """Why `step.depth > 0` cannot be pinned, recorded so nobody re-chases it.

        Mutating that guard to `>= 0` survives, and it is an equivalent mutant rather than
        a gap: `find_assets_near_event` matches `physical_only`, so every origin is a
        physical type, and the service set the walk counts contains none of them. A depth-0
        entry can never be counted whichever way the guard reads.

        The guard is kept because it stops being redundant the moment anything matches a
        logical entity, and this test states the assumption it depends on.
        """
        matches = find_assets_near_event(atlaspay_graph, taiwan_earthquake())
        assert matches
        service_types = {"MICROSERVICE", "APPLICATION", "BUSINESS_SERVICE", "DATABASE"}
        for match in matches:
            assert match.entity.type.value not in service_types

    def test_an_event_matching_nothing_summarises_to_zero(self, atlaspay_graph: WorldGraph):
        """The `if exposed_ids` branch, which no test reached."""
        assert proximity_summary(atlaspay_graph, demo_vulnerability()) == {
            "critical_facilities": 0,
            "suppliers": 0,
            "dependent_services": 0,
            "assets_in_radius": 0,
        }
