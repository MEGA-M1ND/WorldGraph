"""API surface and the end-to-end hero-flow integration test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import RunMode, Settings
from app.storage.repository import SqliteRepository


class TestMeta:
    def test_health(self, client: TestClient):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["entities"] > 0
        assert body["feeds"]

    def test_config_exposes_no_secret(self, client: TestClient):
        body = client.get("/api/config").json()
        assert body["ai_enabled"] is False
        assert "anthropic_api_key" not in body
        assert "api_key" not in str(body).lower().replace("google_maps_api_key", "")

    def test_config_reports_the_analyst_backend(self, client: TestClient):
        analyst = client.get("/api/config").json()["analyst"]
        assert analyst["engine"] == "deterministic"
        assert analyst["available"] is True

    def test_dashboard_labels_the_data_as_synthetic(self, client: TestClient):
        body = client.get("/api/dashboard").json()
        assert "synthetic" in body["data_disclaimer"].lower()
        assert body["organization"] == "AtlasPay"
        assert body["critical_services"] > 0

    def test_feeds_report_state_and_mode(self, client: TestClient):
        feeds = client.get("/api/feeds").json()
        assert feeds
        for feed in feeds:
            assert feed["state"] in {
                "LOADING", "LIVE", "DEGRADED", "STALE", "FALLBACK", "SIMULATED", "UNAVAILABLE"
            }
            assert feed["mode"] in {"LIVE", "REPLAY", "SIMULATED", "SYNTHETIC"}

    def test_timeline_records_startup(self, client: TestClient):
        entries = client.get("/api/timeline").json()
        assert entries
        assert any(entry["stage"] == "ingest" for entry in entries)


class TestWorld:
    def test_world_returns_the_estate_and_metrics(self, client: TestClient):
        body = client.get("/api/world").json()
        assert len(body["entities"]) >= 40
        assert len(body["edges"]) >= 50
        assert body["metrics"]["disclaimer"] == "MODELLED ESTIMATE"

    def test_every_entity_is_labelled_synthetic(self, client: TestClient):
        for entity in client.get("/api/world").json()["entities"]:
            assert entity["source"]["mode"] == "SYNTHETIC"

    def test_entity_detail_has_everything_the_panel_needs(self, client: TestClient):
        body = client.get("/api/world/entities/payments-k8s-singapore").json()
        assert body["entity"]["name"] == "payments-k8s-singapore"
        assert body["depends_on"]
        assert body["dependents"]
        assert set(body["hosts"]) >= {"payments-api", "admin-api"}
        assert body["availability"] == 1.0

    def test_unknown_entity_returns_a_specific_404(self, client: TestClient):
        response = client.get("/api/world/entities/nope")
        assert response.status_code == 404
        assert "No entity 'nope' exists" in response.json()["detail"]
        assert "something went wrong" not in response.json()["detail"].lower()

    def test_material_risks_are_derived_and_explained(self, client: TestClient):
        risks = client.get("/api/world/risks").json()
        assert risks
        for risk in risks:
            assert risk["contributions"], "a risk with no derivation is not shippable"
            assert risk["focus_entity_ids"]

    def test_trace_in_both_directions(self, client: TestClient):
        dependents = client.get(
            "/api/world/trace/payments-k8s-singapore", params={"direction": "dependents"}
        ).json()
        dependencies = client.get(
            "/api/world/trace/payments-k8s-singapore", params={"direction": "dependencies"}
        ).json()
        assert "payments-api" in [s["entity_id"] for s in dependents["steps"]]
        assert "cloud-region-singapore" in [s["entity_id"] for s in dependencies["steps"]]

    def test_trace_reports_truncation(self, client: TestClient):
        body = client.get(
            "/api/world/trace/cloud-region-singapore", params={"max_depth": 1}
        ).json()
        assert body["truncated"] is True
        assert body["truncation_reason"]


class TestEvents:
    def test_events_are_returned_newest_first(self, client: TestClient):
        events = client.get("/api/events").json()
        assert events
        stamps = [e["occurred_at"] for e in events]
        assert stamps == sorted(stamps, reverse=True)

    def test_demo_events_are_never_labelled_live(self, client: TestClient):
        for event in client.get("/api/events").json():
            assert event["source"]["mode"] in {"REPLAY", "SYNTHETIC"}

    def test_event_detail_includes_assets_in_radius(self, client: TestClient):
        body = client.get("/api/events/replay:taiwan-m68").json()
        assert body["proximity"]["suppliers"] >= 1
        ids = {row["entity_id"] for row in body["assets_in_radius"]}
        assert "supplier-taiwan-hardware" in ids

    def test_vulnerability_event_lists_affected_assets(self, client: TestClient):
        body = client.get("/api/events/replay:cve-2026-demo-001").json()
        assert body["vulnerable_assets"]
        assert any(row["internet_facing"] for row in body["vulnerable_assets"])

    def test_replay_catalog_offers_the_three_scenarios(self, client: TestClient):
        scenarios = client.get("/api/events/scenarios").json()
        assert {s["id"] for s in scenarios} == {
            "taiwan-earthquake",
            "singapore-region-outage",
            "critical-cve",
        }

    def test_unknown_event_returns_a_specific_404(self, client: TestClient):
        response = client.get("/api/events/nope")
        assert response.status_code == 404
        assert "is known to WorldGraph" in response.json()["detail"]


class TestAnalysis:
    def test_blast_radius_requires_an_origin(self, client: TestClient):
        response = client.post("/api/analysis/blast-radius", json={})
        assert response.status_code == 422
        assert "entity_ids or an event_id" in response.json()["detail"]

    def test_blast_radius_rejects_unknown_entities(self, client: TestClient):
        response = client.post("/api/analysis/blast-radius", json={"entity_ids": ["ghost"]})
        assert response.status_code == 404
        assert "Unknown entities" in response.json()["detail"]

    def test_attack_paths_carry_the_disclaimer(self, client: TestClient):
        body = client.get(
            "/api/analysis/security/attack-paths", params={"to_entity_id": "payments-api"}
        ).json()
        assert body["count"] >= 1
        assert "not exploitability" in body["disclaimer"]

    def test_response_plan_without_analysis_says_so(self, client: TestClient):
        response = client.post("/api/analysis/response-plan", json={})
        assert response.status_code == 422
        assert "Analyse an event or a scenario first" in response.json()["detail"]


class TestSimulation:
    def test_scenario_crud(self, client: TestClient):
        created = client.post("/api/simulation", json={"name": "crud"}).json()
        scenario_id = created["id"]
        assert created["mode"] == "SIMULATED"

        added = client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "cloud-region-singapore", "health": "DOWN"},
        ).json()
        assert len(added["overrides"]) == 1
        override_id = added["overrides"][0]["id"]

        removed = client.delete(
            f"/api/simulation/{scenario_id}/overrides/{override_id}"
        ).json()
        assert removed["overrides"] == []

        assert client.delete(f"/api/simulation/{scenario_id}").status_code == 204
        assert client.get(f"/api/simulation/{scenario_id}").status_code == 404

    def test_override_on_unknown_entity_is_rejected(self, client: TestClient):
        scenario_id = client.post("/api/simulation", json={"name": "x"}).json()["id"]
        response = client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "ghost", "health": "DOWN"},
        )
        assert response.status_code == 404
        assert "unknown entity" in response.json()["detail"]

    def test_capacity_override_needs_a_capacity(self, client: TestClient):
        scenario_id = client.post("/api/simulation", json={"name": "x"}).json()["id"]
        response = client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "payments-api", "kind": "ENTITY_CAPACITY"},
        )
        assert response.status_code == 422

    def test_reset_clears_overrides(self, client: TestClient):
        scenario_id = client.post("/api/simulation", json={"name": "x"}).json()["id"]
        client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "payments-api", "health": "DOWN"},
        )
        assert client.post(f"/api/simulation/{scenario_id}/reset").json()["overrides"] == []


class TestRateLimiting:
    def test_ai_endpoint_is_rate_limited(self):
        from app.main import create_app

        settings = Settings(
            run_mode=RunMode.DEMO, database_path=":memory:", ai_rate_limit_per_minute=3
        )
        app = create_app(settings, SqliteRepository(":memory:"))
        with TestClient(app) as client:
            payload = {"message": "What can hurt us right now?"}
            codes = [client.post("/api/ai/ask", json=payload).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes[3:]

    def test_rate_limited_response_says_when_to_retry(self):
        from app.main import create_app

        settings = Settings(
            run_mode=RunMode.DEMO, database_path=":memory:", ai_rate_limit_per_minute=1
        )
        app = create_app(settings, SqliteRepository(":memory:"))
        with TestClient(app) as client:
            client.post("/api/ai/ask", json={"message": "hi"})
            response = client.post("/api/ai/ask", json={"message": "hi"})
        assert response.status_code == 429
        assert "Retry-After" in response.headers
        assert "Try again in" in response.json()["detail"]


class TestAiEndpoints:
    def test_tool_surface_is_public_and_bounded(self, client: TestClient):
        tools = client.get("/api/ai/tools").json()
        names = {tool["name"] for tool in tools}
        assert "calculate_blast_radius" in names
        assert not names & {"execute", "shell", "http_get", "eval"}

    def test_ask_validates_its_input(self, client: TestClient):
        assert client.post("/api/ai/ask", json={"message": ""}).status_code == 422
        assert client.post("/api/ai/ask", json={"message": "x" * 5000}).status_code == 422
        assert client.post("/api/ai/ask", json={"message": "hi", "evil": 1}).status_code == 422


class TestHeroFlow:
    """The Definition of Done for V1, executed end to end against the real API.

    Every step in the specification's 16-point sequence, in order, with the assertions
    that make each step meaningful rather than merely non-crashing.
    """

    def test_full_hero_demo(self, client: TestClient):
        # 1-2. Launch and see the AtlasPay estate.
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["organization"] == "AtlasPay"
        assert dashboard["critical_services"] >= 5
        assert dashboard["infrastructure_assets"] >= 30
        assert dashboard["material_risks"] >= 3

        world = client.get("/api/world").json()
        assert len(world["entities"]) >= 40
        assert world["metrics"]["availability"] == 1.0

        # 3-4. See and select the Taiwan earthquake.
        events = client.get("/api/events").json()
        taiwan = next(e for e in events if "Hsinchu" in e["title"])
        assert taiwan["source"]["mode"] == "REPLAY", "a fixture must never read as LIVE"
        detail = client.get(f"/api/events/{taiwan['id']}").json()
        assert detail["proximity"]["suppliers"] >= 1
        assert detail["proximity"]["dependent_services"] >= 1

        # 5-6. Analyse impact; see directly exposed assets.
        analysis = client.post(
            "/api/analysis/blast-radius", json={"event_id": taiwan["id"]}
        ).json()
        direct_ids = {row["entity_id"] for row in analysis["direct_impact"]}
        assert "supplier-taiwan-hardware" in direct_ids

        # 7. See dependency propagation reaching customers.
        indirect_ids = {row["entity_id"] for row in analysis["indirect_impact"]}
        assert "payments-api" in indirect_ids
        assert "customers-apac" in indirect_ids
        assert analysis["critical_paths"]

        # 8. See a HIGH risk explanation with its full derivation.
        assert analysis["severity"] == "HIGH"
        assert analysis["risk"]["contributions"]
        assert analysis["explanations"]
        assert analysis["confidence"]["strong_evidence"]
        assert analysis["confidence"]["uncertainties"]

        # 9-10. Enter simulation mode; mark the Taiwan supplier unavailable.
        scenario_id = client.post(
            "/api/simulation",
            json={"name": "Taiwan supplier unavailable", "origin_event_id": taiwan["id"]},
        ).json()["id"]
        client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "supplier-taiwan-hardware", "health": "DOWN"},
        )
        first = client.get(f"/api/simulation/{scenario_id}/compare").json()
        assert first["simulated"]["material_risk"] == "HIGH"
        assert first["simulated"]["regional_capacity"]["APAC"] < 0.8

        # 11. Add the Singapore payments outage.
        client.post(
            f"/api/simulation/{scenario_id}/overrides",
            json={"target_id": "payments-k8s-singapore", "health": "DOWN"},
        )
        second = client.get(f"/api/simulation/{scenario_id}/compare").json()

        # 12. Baseline versus simulated.
        assert second["baseline"]["availability"] == 1.0
        assert second["simulated"]["availability"] < first["simulated"]["availability"]
        assert second["simulated"]["material_risk"] == "CRITICAL"
        deltas = {row["key"]: row for row in second["deltas"]}
        assert deltas["availability"]["direction"] == "worse"
        assert deltas["material_risk"]["baseline"] == "LOW"
        assert second["cascade_paths"]

        # The real world must be untouched by all of that.
        assert client.get("/api/world").json()["metrics"]["availability"] == 1.0

        # 13-14. Ask "What should we do?" and receive a structured plan.
        answer = client.post(
            "/api/ai/ask",
            json={"message": "What should we do?", "active_scenario_id": scenario_id},
        ).json()
        assert answer["engine"] == "deterministic"
        assert "generate_response_plan" in [call["tool"] for call in answer["tool_calls"]]

        plan = client.post(
            "/api/analysis/response-plan", json={"scenario_id": scenario_id}
        ).json()
        assert len(plan["actions"]) >= 3

        # 15. Every recommendation includes a rationale.
        for action in plan["actions"]:
            assert action["rationale"].strip()
            assert action["urgency"] in {"NOW", "SOON", "MONITOR"}

        # 16. Nothing was executed.
        for action in plan["actions"]:
            assert action["executed"] is False
        assert "executes nothing" in plan["summary"]

        # The timeline records the whole investigation, so it is inspectable.
        stages = {entry["stage"] for entry in client.get("/api/timeline").json()}
        assert {"normalize", "correlate", "analyze", "risk", "simulate", "plan"} <= stages

    def test_security_hero_flow(self, client: TestClient):
        """The second hero scenario: vulnerability → reachability → business impact."""
        exposure = client.get("/api/events/replay:cve-2026-demo-001").json()
        assert len(exposure["vulnerable_assets"]) >= 3
        internet_facing = [a for a in exposure["vulnerable_assets"] if a["internet_facing"]]
        assert len(internet_facing) == 1, "only admin-api should be internet-facing"

        paths = client.get(
            "/api/analysis/security/attack-paths", params={"to_entity_id": "payments-api"}
        ).json()
        assert paths["count"] >= 1
        shortest = paths["paths"][0]["ids"]
        assert shortest == ["internet", "admin-api", "internal-auth", "payments-api"]

        analysis = client.post(
            "/api/analysis/blast-radius", json={"event_id": "replay:cve-2026-demo-001"}
        ).json()
        assert analysis["origin_kind"] == "SECURITY_FINDING"
        assert "admin-api" in analysis["origin_ids"]
        assert analysis["business_impact"]["disclaimer"] == "MODELLED ESTIMATE"

    def test_hero_flow_is_reproducible(self, client: TestClient):
        """Same clicks, same numbers — the whole point of a deterministic fixture."""
        first = client.post(
            "/api/analysis/blast-radius", json={"event_id": "replay:taiwan-m68"}
        ).json()
        second = client.post(
            "/api/analysis/blast-radius", json={"event_id": "replay:taiwan-m68"}
        ).json()
        assert first["risk"]["score"] == second["risk"]["score"]
        assert first["severity"] == second["severity"]
        assert [r["entity_id"] for r in first["direct_impact"]] == [
            r["entity_id"] for r in second["direct_impact"]
        ]


class TestPersistence:
    def test_estate_survives_a_restart(self):
        """A shared repository is reloaded rather than re-seeded from the fixture."""
        from app.main import create_app

        repository = SqliteRepository(":memory:")
        settings = Settings(run_mode=RunMode.DEMO, database_path=":memory:")

        with TestClient(create_app(settings, repository)) as client:
            before = client.get("/api/dashboard").json()["entities"]
        with TestClient(create_app(settings, repository)) as client:
            assert client.get("/api/dashboard").json()["entities"] == before

    def test_analyses_are_persisted_and_retrievable(self, client: TestClient):
        analysis_id = client.post(
            "/api/analysis/blast-radius", json={"event_id": "replay:taiwan-m68"}
        ).json()["id"]
        assert client.get(f"/api/analysis/{analysis_id}").status_code == 200
