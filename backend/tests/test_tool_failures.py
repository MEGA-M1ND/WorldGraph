"""What a tool says when it cannot do what was asked.

Coverage measurement found most of `app/ai/tools.py`'s error paths never executed by any
test — roughly forty statements, almost all of them `raise ToolError(...)`. Mutation
testing had nothing to say about them either: a line no test runs cannot have a mutant
killed.

These are the messages a model sees when it asks for something that does not exist, and
they are the tool layer's half of the product's central claim. A tool that fails silently,
or returns something plausible instead of an error, is an invitation to hallucinate around
the gap. Every failure below must name what was missing and must not answer a different
question instead.

The write tools are here too — the only four that change anything, and they change only a
*scenario*. Their argument validation and their "this is a hypothetical" notes were
uncovered.
"""

from __future__ import annotations

import pytest

from app.ai.tools import TOOLS, ToolContext, ToolError, run_tool


@pytest.fixture
def ctx(world) -> ToolContext:
    return ToolContext(state=world)


def _an_entity(ctx: ToolContext) -> str:
    return next(e.id for e in ctx.state.entities())


def _an_event(ctx: ToolContext) -> str:
    return next(iter(ctx.state._events))


# ======================================================================================
# Nothing is invented in place of a missing thing
# ======================================================================================


class TestAnUnknownIdIsAnErrorNotAnAnswer:
    @pytest.mark.parametrize(
        ("tool", "args", "expected"),
        [
            ("get_entity", {"entity_id": "no-such-entity"}, "No entity 'no-such-entity'"),
            ("get_event", {"event_id": "no-such-event"}, "No event 'no-such-event'"),
            ("trace_dependencies", {"entity_id": "no-such-entity"}, "No entity 'no-such-entity'"),
            ("trace_dependents", {"entity_id": "no-such-entity"}, "No entity 'no-such-entity'"),
            ("focus_entity", {"entity_id": "no-such-entity"}, "No entity 'no-such-entity'"),
            ("focus_event", {"event_id": "no-such-event"}, "No event 'no-such-event'"),
            (
                "find_assets_near_event",
                {"event_id": "no-such-event"},
                "No event 'no-such-event'",
            ),
            (
                "compare_simulation",
                {"scenario_id": "scn-nope"},
                "No simulation scenario 'scn-nope'",
            ),
            (
                "reset_simulation",
                {"scenario_id": "scn-nope"},
                "No simulation scenario 'scn-nope'",
            ),
            (
                "add_simulation_override",
                {"scenario_id": "scn-nope", "target_id": "a", "health": "DOWN"},
                "No simulation scenario 'scn-nope'",
            ),
            (
                "remove_simulation_override",
                {"scenario_id": "scn-nope", "override_id": "ov-1"},
                "No simulation scenario 'scn-nope'",
            ),
            (
                "generate_response_plan",
                {"analysis_id": "blast-nope"},
                "No analysis 'blast-nope'",
            ),
            (
                "generate_response_plan",
                {"scenario_id": "scn-nope"},
                "No simulation scenario 'scn-nope'",
            ),
            ("annotate_entities", {"entity_ids": ["no-such-entity"]}, "Unknown entities"),
            (
                "calculate_blast_radius",
                {"entity_ids": ["no-such-entity"]},
                "Unknown entities",
            ),
            (
                "calculate_blast_radius",
                {"scenario_id": "scn-nope"},
                "No simulation scenario 'scn-nope'",
            ),
        ],
    )
    def test_the_error_names_what_was_missing(self, ctx, tool, args, expected):
        with pytest.raises(ToolError) as error:
            run_tool(ctx, tool, args)
        assert expected in str(error.value)

    def test_no_failure_message_carries_a_stack_trace_or_a_path(self, ctx):
        """Tool errors are shown to a model and to a user. Both are untrusted readers."""
        for tool, args in (
            ("get_entity", {"entity_id": "no-such-entity"}),
            ("compare_simulation", {"scenario_id": "scn-nope"}),
        ):
            with pytest.raises(ToolError) as error:
                run_tool(ctx, tool, args)
            message = str(error.value)
            assert "Traceback" not in message
            assert "/home/" not in message
            assert "app/ai/tools.py" not in message


class TestAMissingSelectionIsStatedNotGuessed:
    def test_with_nothing_selected_the_tool_says_so(self, ctx):
        result = run_tool(ctx, "get_selected_entity", {})
        assert result["selected"] is None
        assert result["note"] == "No entity is currently selected in the UI."

    def test_with_something_selected_it_returns_that_entity(self, world):
        entity_id = next(e.id for e in world.entities())
        ctx = ToolContext(state=world, selected_entity_id=entity_id)
        result = run_tool(ctx, "get_selected_entity", {})
        assert result["selected"]["id"] == entity_id
        assert "note" not in result

    def test_a_blast_radius_with_no_origin_and_no_selection_refuses(self, ctx):
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "calculate_blast_radius", {})
        assert "nothing is currently selected" in str(error.value)

    def test_a_selection_is_used_as_the_origin_when_no_ids_are_given(self, world):
        entity_id = next(e.id for e in world.entities())
        ctx = ToolContext(state=world, selected_entity_id=entity_id)
        result = run_tool(ctx, "calculate_blast_radius", {})
        assert result["origin_ids"] == [entity_id]

    def test_a_plan_with_no_analysis_anywhere_says_to_analyse_something_first(self, world):
        world._analyses.clear()
        ctx = ToolContext(state=world)
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "generate_response_plan", {})
        assert "No analysis has been run yet" in str(error.value)


class TestGeographyToolsRefuseWhatTheyCannotPlace:
    @pytest.mark.anyio
    async def test_an_entity_with_no_location_cannot_be_flown_to(self, world):
        from app.graph.world_graph import WorldGraph
        from app.models.core import (
            BusinessProfile,
            DataMode,
            DataSourceInfo,
            EntityType,
            ExposureProfile,
            WorldEntity,
        )

        world.graph = WorldGraph(
            [
                WorldEntity(
                    id="nowhere",
                    type=EntityType.APPLICATION,
                    name="nowhere",
                    source=DataSourceInfo(source_id="s", source_name="S", mode=DataMode.LIVE),
                    location=None,
                    business=BusinessProfile(region="westeurope"),
                    exposure=ExposureProfile(internet_facing=False, network_zone="z"),
                )
            ],
            [],
        )
        ctx = ToolContext(state=world)
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "focus_entity", {"entity_id": "nowhere"})
        assert "has no location, so the camera cannot fly to it" in str(error.value)
        assert ctx.directives == [], "a refused fly-to must not emit a camera move"

    @pytest.mark.anyio
    async def test_an_event_with_no_location_cannot_be_flown_to_either(self, world):
        event = world._events[_an_event(ToolContext(state=world))]
        event.location = None
        ctx = ToolContext(state=world)
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "focus_event", {"event_id": event.id})
        assert "has no location" in str(error.value)
        assert ctx.directives == []

    @pytest.mark.anyio
    async def test_a_located_entity_does_emit_the_camera_move(self, world):
        located = next(e for e in world.entities() if e.location is not None)
        ctx = ToolContext(state=world)
        result = run_tool(ctx, "focus_entity", {"entity_id": located.id})
        assert result["focused"] == located.id
        assert ctx.directives == [{"kind": "focus_entity", "entity_id": located.id}]


class TestDependencyPathsAreFoundOrDeclared:
    @pytest.mark.anyio
    async def test_two_unconnected_entities_yield_an_error_not_an_empty_path(self, world):
        ctx = ToolContext(state=world)
        ids = [e.id for e in world.entities()]
        with pytest.raises(ToolError) as error:
            run_tool(
                ctx,
                "show_dependency_path",
                {"from_entity_id": ids[0], "to_entity_id": "no-such-entity"},
            )
        assert "No dependency path connects" in str(error.value)
        assert ctx.directives == []

    @pytest.mark.anyio
    async def test_a_real_path_names_its_direction_and_its_hops(self, world):
        """`depends-on` and `impact` are opposite readings of the same edge chain."""
        ctx = ToolContext(state=world)
        result = run_tool(
            ctx,
            "show_dependency_path",
            {"from_entity_id": "checkout-platform", "to_entity_id": "cloud-region-singapore"},
        )
        assert result["direction"] in {"depends-on", "impact"}
        assert result["path"][0] == "checkout-platform"
        assert result["path"][-1] == "cloud-region-singapore"
        assert len(result["names"]) == len(result["path"])
        assert ctx.directives == [{"kind": "show_path", "path": result["path"]}]


# ======================================================================================
# The four tools that change anything
# ======================================================================================


class TestTheWriteToolsChangeOnlyAScenario:
    @pytest.mark.anyio
    async def test_an_override_needs_a_health_state_or_a_capacity(self, world):
        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        with pytest.raises(ToolError) as error:
            run_tool(
                ctx,
                "add_simulation_override",
                {"scenario_id": scenario["scenario_id"], "target_id": _an_entity(ctx)},
            )
        assert "needs either a health state or a capacity value" in str(error.value)

    @pytest.mark.anyio
    async def test_an_unrecognised_health_state_lists_the_valid_ones(self, world):
        """So a model can correct itself rather than guessing again."""
        from app.models.core import HealthState

        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        with pytest.raises(ToolError) as error:
            run_tool(
                ctx,
                "add_simulation_override",
                {
                    "scenario_id": scenario["scenario_id"],
                    "target_id": _an_entity(ctx),
                    "health": "EXPLODED",
                },
            )
        message = str(error.value)
        assert "'EXPLODED' is not a health state" in message
        for state in HealthState:
            assert state.value in message

    @pytest.mark.anyio
    async def test_an_override_on_an_unknown_target_is_rejected_by_validation(self, world):
        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        with pytest.raises(ToolError):
            run_tool(
                ctx,
                "add_simulation_override",
                {
                    "scenario_id": scenario["scenario_id"],
                    "target_id": "no-such-entity",
                    "health": "DOWN",
                },
            )
        assert run_tool(ctx, "compare_simulation", {"scenario_id": scenario["scenario_id"]})

    @pytest.mark.anyio
    async def test_adding_an_override_says_it_is_hypothetical(self, world):
        """§27 again: the tool result itself must say the real world is unchanged."""
        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        added = run_tool(
            ctx,
            "add_simulation_override",
            {
                "scenario_id": scenario["scenario_id"],
                "target_id": _an_entity(ctx),
                "health": "DOWN",
            },
        )
        assert added["note"] == "This is a hypothetical. The real world state is unchanged."
        assert added["override_id"].startswith("ovr-")
        assert len(added["overrides"]) == 1

    @pytest.mark.anyio
    async def test_a_capacity_override_is_accepted_and_described(self, world):
        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        added = run_tool(
            ctx,
            "add_simulation_override",
            {
                "scenario_id": scenario["scenario_id"],
                "target_id": _an_entity(ctx),
                "capacity": 0.4,
            },
        )
        assert "40%" in added["added"]

    @pytest.mark.anyio
    async def test_removing_an_override_that_is_not_there_is_an_error(self, world):
        ctx = ToolContext(state=world)
        scenario = run_tool(ctx, "create_simulation", {"name": "probe"})
        with pytest.raises(ToolError) as error:
            run_tool(
                ctx,
                "remove_simulation_override",
                {"scenario_id": scenario["scenario_id"], "override_id": "ov-never-added"},
            )
        assert "has no override 'ov-never-added'" in str(error.value)

    @pytest.mark.anyio
    async def test_removing_an_override_leaves_the_others(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        targets = [e.id for e in world.entities()][:2]
        added = [
            run_tool(
                ctx,
                "add_simulation_override",
                {"scenario_id": scenario_id, "target_id": target, "health": "DOWN"},
            )["override_id"]
            for target in targets
        ]
        left = run_tool(
            ctx,
            "remove_simulation_override",
            {"scenario_id": scenario_id, "override_id": added[0]},
        )
        assert len(left["overrides"]) == 1

    @pytest.mark.anyio
    async def test_resetting_clears_every_override_and_leaves_simulation_mode(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        run_tool(
            ctx,
            "add_simulation_override",
            {"scenario_id": scenario_id, "target_id": _an_entity(ctx), "health": "DOWN"},
        )
        reset = run_tool(ctx, "reset_simulation", {"scenario_id": scenario_id})
        assert reset["overrides"] == []
        assert reset["note"] == "Scenario reset to baseline."
        assert {"kind": "simulation_mode", "scenario_id": scenario_id, "active": False} in (
            ctx.directives
        )

    @pytest.mark.anyio
    async def test_a_write_tool_never_touches_the_real_graph(self, world):
        """The whole point of the write allowlist."""
        before = {e.id: e.health for e in world.entities()}
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        run_tool(
            ctx,
            "add_simulation_override",
            {"scenario_id": scenario_id, "target_id": _an_entity(ctx), "health": "DOWN"},
        )
        run_tool(ctx, "compare_simulation", {"scenario_id": scenario_id})
        assert {e.id: e.health for e in world.entities()} == before


class TestFilteringAndWindowing:
    @pytest.mark.anyio
    async def test_the_entity_type_filter_is_case_insensitive_and_exact(self, world):
        ctx = ToolContext(state=world)
        lower = run_tool(ctx, "search_entities", {"entity_type": "microservice", "limit": 40})
        upper = run_tool(ctx, "search_entities", {"entity_type": "MICROSERVICE", "limit": 40})
        assert lower["count"] == upper["count"] > 0
        assert all(row["type"] == "MICROSERVICE" for row in lower["entities"])

    @pytest.mark.anyio
    async def test_a_recency_window_actually_narrows_the_event_list(self, world):
        """The replay events are historical, so a short window must come back empty."""
        ctx = ToolContext(state=world)
        assert run_tool(ctx, "list_recent_events", {})["count"] > 0
        assert run_tool(ctx, "list_recent_events", {"minutes": 5})["count"] == 0

    @pytest.mark.anyio
    async def test_every_registered_tool_dispatches_rather_than_being_unreachable(self, world):
        """A tool in the registry that cannot be run is worse than one that is absent.

        A `ToolError` here is fine — most of these need arguments. What must never happen
        is the *dispatch* failing, which is a different message, or an exception type that
        is not `ToolError` escaping to the model.
        """
        ctx = ToolContext(state=world)
        for name in TOOLS:
            try:
                run_tool(ctx, name, {})
            except ToolError as error:
                assert "is not an available tool" not in str(error), name

    @pytest.mark.anyio
    async def test_an_unregistered_name_is_refused_and_lists_what_exists(self, world):
        ctx = ToolContext(state=world)
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "delete_production", {})
        message = str(error.value)
        assert "'delete_production' is not an available tool" in message
        # It names the allowlist, so a model can correct itself instead of guessing.
        assert "get_entity" in message


class TestScenarioBackedBlastRadius:
    @pytest.mark.anyio
    async def test_an_empty_scenario_has_no_blast_radius_and_says_so(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "calculate_blast_radius", {"scenario_id": scenario_id})
        assert "no failures in it yet" in str(error.value)

    @pytest.mark.anyio
    async def test_an_empty_scenario_has_nothing_to_plan_for_either(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        with pytest.raises(ToolError) as error:
            run_tool(ctx, "generate_response_plan", {"scenario_id": scenario_id})
        assert "nothing to plan for" in str(error.value)

    @pytest.mark.anyio
    async def test_a_scenario_with_a_failure_produces_a_simulated_blast_radius(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "probe"})["scenario_id"]
        run_tool(
            ctx,
            "add_simulation_override",
            {"scenario_id": scenario_id, "target_id": "cloud-region-singapore",
             "health": "DOWN"},
        )
        result = run_tool(ctx, "calculate_blast_radius", {"scenario_id": scenario_id})
        assert result["direct_impact"] or result["indirect_impact"]
        # A hypothetical must never render as a live measurement.
        assert result["mode"] == "SIMULATED"

    @pytest.mark.anyio
    async def test_a_scenario_plan_is_named_after_its_scenario(self, world):
        ctx = ToolContext(state=world)
        scenario_id = run_tool(ctx, "create_simulation", {"name": "Singapore loss"})[
            "scenario_id"
        ]
        run_tool(
            ctx,
            "add_simulation_override",
            {"scenario_id": scenario_id, "target_id": "cloud-region-singapore",
             "health": "DOWN"},
        )
        plan = run_tool(ctx, "generate_response_plan", {"scenario_id": scenario_id})
        assert plan["actions"]
        assert all(action["executed"] is False for action in plan["actions"])


class TestTheHappyPathsThatWereAlsoUncovered:
    @pytest.mark.anyio
    async def test_a_radius_override_widens_the_search_without_changing_the_event(self, world):
        ctx = ToolContext(state=world)
        event = next(e for e in world.events() if e.location is not None)
        narrow = run_tool(
            ctx, "find_assets_near_event", {"event_id": event.id, "radius_km": 1.0}
        )
        wide = run_tool(
            ctx, "find_assets_near_event", {"event_id": event.id, "radius_km": 5000.0}
        )
        assert wide["count"] >= narrow["count"]
        assert wide["radius_km"] == 5000.0
        # The event itself is untouched: this is a query parameter, not a mutation.
        assert world.event(event.id).exposure_radius_km == event.exposure_radius_km

    @pytest.mark.anyio
    async def test_omitting_the_radius_uses_the_events_own(self, world):
        ctx = ToolContext(state=world)
        event = next(e for e in world.events() if e.exposure_radius_km > 0)
        result = run_tool(ctx, "find_assets_near_event", {"event_id": event.id})
        assert result["radius_km"] == event.exposure_radius_km

    @pytest.mark.anyio
    async def test_annotating_known_entities_emits_the_directive(self, world):
        ctx = ToolContext(state=world)
        ids = [e.id for e in world.entities()][:2]
        result = run_tool(ctx, "annotate_entities", {"entity_ids": ids, "label": "probe"})
        assert result["annotated"] == ids
        assert ctx.directives == [
            {"kind": "annotate", "entity_ids": ids, "label": "probe"}
        ]


class TestTheRegistryAndItsErrorTranslation:
    def test_a_duplicate_tool_name_is_refused_at_registration(self):
        """Two tools under one name would make the allowlist ambiguous."""
        from app.ai.tools import Tool, _register

        existing = TOOLS["get_entity"]
        with pytest.raises(RuntimeError) as error:
            _register(
                Tool(
                    name="get_entity",
                    description="a second one",
                    args_model=existing.args_model,
                    handler=existing.handler,
                )
            )
        assert "duplicate tool 'get_entity'" in str(error.value)

    @pytest.mark.anyio
    async def test_an_unexpected_exception_reaches_the_model_as_a_type_name_only(self, world):
        """A raw exception message could carry a path, a query or a credential.

        `run_tool` is the last boundary before a string is handed to a language model, so
        only the exception's *type* is allowed through.
        """
        ctx = ToolContext(state=world)

        def exploding(_ctx, _args):
            raise ValueError(
                "connection to postgres://admin:hunter2@10.0.0.5/prod failed at /srv/app.py"
            )

        original = TOOLS["get_world_status"].handler
        object.__setattr__(TOOLS["get_world_status"], "handler", exploding)
        try:
            with pytest.raises(ToolError) as error:
                run_tool(ctx, "get_world_status", {})
        finally:
            object.__setattr__(TOOLS["get_world_status"], "handler", original)

        message = str(error.value)
        assert message == "get_world_status failed: ValueError."
        for leak in ("postgres://", "hunter2", "10.0.0.5", "/srv/app.py"):
            assert leak not in message

    @pytest.mark.anyio
    async def test_a_missing_key_is_translated_rather_than_escaping_raw(self, world):
        ctx = ToolContext(state=world)

        def missing_key(_ctx, _args):
            raise KeyError("workspace")

        original = TOOLS["get_world_status"].handler
        object.__setattr__(TOOLS["get_world_status"], "handler", missing_key)
        try:
            with pytest.raises(ToolError) as error:
                run_tool(ctx, "get_world_status", {})
        finally:
            object.__setattr__(TOOLS["get_world_status"], "handler", original)
        assert "get_world_status could not find" in str(error.value)

    @pytest.mark.anyio
    async def test_a_simulation_error_keeps_its_own_wording(self, world):
        """It is already user-safe and says which override was rejected."""
        from app.simulation.engine import SimulationError

        ctx = ToolContext(state=world)

        def rejecting(_ctx, _args):
            raise SimulationError("no entity 'ghost' to override")

        original = TOOLS["get_world_status"].handler
        object.__setattr__(TOOLS["get_world_status"], "handler", rejecting)
        try:
            with pytest.raises(ToolError) as error:
                run_tool(ctx, "get_world_status", {})
        finally:
            object.__setattr__(TOOLS["get_world_status"], "handler", original)
        assert str(error.value) == "no entity 'ghost' to override"
