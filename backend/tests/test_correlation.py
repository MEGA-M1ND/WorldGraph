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
from app.models.core import EventCategory, GeoPoint

SINGAPORE = GeoPoint(lat=1.3521, lon=103.8198)
FRANKFURT = GeoPoint(lat=50.1109, lon=8.6821)
HSINCHU = GeoPoint(lat=24.7736, lon=120.9417)


class TestDistance:
    def test_identical_points_are_zero_apart(self):
        assert haversine_km(SINGAPORE, SINGAPORE) == pytest.approx(0.0, abs=1e-9)

    def test_known_distance(self):
        """Singapore → Frankfurt is ~10,270 km great-circle."""
        assert haversine_km(SINGAPORE, FRANKFURT) == pytest.approx(10_270, rel=0.01)

    def test_symmetry(self):
        assert haversine_km(SINGAPORE, FRANKFURT) == pytest.approx(
            haversine_km(FRANKFURT, SINGAPORE)
        )

    def test_antipodal_does_not_overflow(self):
        """The sqrt clamp exists for exactly this input."""
        north = GeoPoint(lat=90.0, lon=0.0)
        south = GeoPoint(lat=-90.0, lon=0.0)
        assert haversine_km(north, south) == pytest.approx(20_015, rel=0.01)

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

    @pytest.mark.parametrize(
        ("bearing", "expected"),
        [(0, "N"), (45, "NE"), (90, "E"), (180, "S"), (270, "W"), (359, "N")],
    )
    def test_compass_labels(self, bearing: float, expected: str):
        assert compass_point(bearing) == expected


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
