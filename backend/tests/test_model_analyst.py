"""The optional model-backed analyst, and what happens when the model is not there.

`app/ai/analyst.py` sat at 54 % coverage: the whole Anthropic tool-use loop, the fallback
path, and both user-facing helper functions were never executed. The deployed default is
the deterministic router, so none of this runs unless an API key is configured — which is
exactly why it needed testing. Code that only runs in a configuration nobody tests is code
that fails the first time somebody uses it.

Three properties matter here, and all three were unprotected:

1. **A model failure degrades, it does not break.** WorldGraph falls back to the
   deterministic analyst and labels the answer with a user-safe reason.
2. **The reason is safe to show.** A raw model-backend exception can carry a URL, a key
   or a request body; only the shape of the failure escapes.
3. **The prompt carries pointers, not the world.** `_selected_context_block` sends ids —
   the model calls a tool if it wants the contents. This is the "do not dump application
   state into the prompt" rule in practice (Reality Pass §26).

No network is touched. The HTTP client is replaced with a fake that returns prepared
Anthropic-shaped responses, which tests the loop that was written — not the API.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.analyst import (
    MAX_TOOL_ROUNDS,
    DeterministicAnalyst,
    ModelAnalyst,
    _describe_model_error,
    _selected_context_block,
    analyst_status,
    build_analyst,
)
from app.ai.tools import ToolContext
from app.config import RunMode, Settings

LEAKY = "https://api.anthropic.com/v1/messages?key=sk-ant-secret123"


def _settings(**kwargs) -> Settings:
    base = {
        "run_mode": RunMode.DEMO,
        "database_path": ":memory:",
        "anthropic_api_key": "sk-ant-test-key",
        "cesium_ion_token": None,
        "google_maps_api_key": None,
    }
    base.update(kwargs)
    return Settings(**base)


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", LEAKY),
                response=httpx.Response(self.status_code, request=httpx.Request("POST", LEAKY)),
            )

    def json(self) -> dict:
        return self._body


class FakeClient:
    """Returns prepared responses in order, recording every request payload."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.payloads.append(json)
        self.headers.append(headers or {})
        index = min(len(self.payloads) - 1, len(self._responses) - 1)
        result = self._responses[index]
        if isinstance(result, Exception):
            raise result
        return result


def _text(text: str) -> FakeResponse:
    return FakeResponse({"content": [{"type": "text", "text": text}]})


def _tool_use(name: str, args: dict, *, use_id: str = "tu-1") -> FakeResponse:
    return FakeResponse(
        {"content": [{"type": "tool_use", "id": use_id, "name": name, "input": args}]}
    )


# ======================================================================================
# The loop
# ======================================================================================


class TestTheToolUseLoop:
    @pytest.mark.anyio
    async def test_a_plain_answer_comes_back_with_no_tool_calls(self, world, monkeypatch):
        client = FakeClient([_text("The Singapore region is healthy.")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        analyst = ModelAnalyst(world, _settings())
        answer = await analyst.ask("how are we doing?")
        assert answer.answer == "The Singapore region is healthy."
        assert answer.tool_calls == []
        assert answer.degraded_reason is None

    @pytest.mark.anyio
    async def test_a_tool_use_is_executed_and_its_result_returned_to_the_model(
        self, world, monkeypatch
    ):
        entity_id = world.entities()[0].id
        client = FakeClient(
            [_tool_use("get_entity", {"entity_id": entity_id}), _text("It is fine.")]
        )
        monkeypatch.setattr(httpx, "AsyncClient", client)
        analyst = ModelAnalyst(world, _settings())
        answer = await analyst.ask("tell me about it")

        assert answer.answer == "It is fine."
        assert [call["tool"] for call in answer.tool_calls] == ["get_entity"]
        assert answer.tool_calls[0]["ok"] is True

        # The second request carries the tool result back as a user turn.
        second = client.payloads[1]["messages"]
        assert second[-1]["role"] == "user"
        result_block = second[-1]["content"][0]
        assert result_block["type"] == "tool_result"
        assert result_block["tool_use_id"] == "tu-1"
        assert entity_id in result_block["content"]

    @pytest.mark.anyio
    async def test_a_tool_error_goes_back_to_the_model_rather_than_ending_the_turn(
        self, world, monkeypatch
    ):
        """The model can correct a bad argument; a raised exception just loses the turn."""
        client = FakeClient(
            [_tool_use("get_entity", {"entity_id": "no-such-entity"}), _text("Sorry.")]
        )
        monkeypatch.setattr(httpx, "AsyncClient", client)
        analyst = ModelAnalyst(world, _settings())
        answer = await analyst.ask("tell me about it")

        assert answer.answer == "Sorry."
        assert answer.tool_calls[0]["ok"] is False
        assert "no-such-entity" in answer.tool_calls[0]["error"]
        result_block = client.payloads[1]["messages"][-1]["content"][0]
        assert result_block["is_error"] is True

    @pytest.mark.anyio
    async def test_an_endless_tool_loop_is_bounded_and_says_so(self, world, monkeypatch):
        """Without the bound, a looping model bills forever and never answers."""
        entity_id = world.entities()[0].id
        client = FakeClient([_tool_use("get_entity", {"entity_id": entity_id})])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        analyst = ModelAnalyst(world, _settings())
        answer = await analyst.ask("loop forever")

        assert len(client.payloads) == MAX_TOOL_ROUNDS
        assert "reached the tool-call limit" in answer.answer
        assert len(answer.tool_calls) == MAX_TOOL_ROUNDS

    @pytest.mark.anyio
    async def test_an_empty_model_response_does_not_render_as_an_empty_answer(
        self, world, monkeypatch
    ):
        client = FakeClient([FakeResponse({"content": []})])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        analyst = ModelAnalyst(world, _settings())
        answer = await analyst.ask("say nothing")
        assert answer.answer == "I could not produce an answer for that."

    @pytest.mark.anyio
    async def test_the_credential_goes_in_a_header_and_never_in_the_body(
        self, world, monkeypatch
    ):
        client = FakeClient([_text("ok")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        await ModelAnalyst(world, _settings()).ask("hello")
        assert client.headers[0]["x-api-key"] == "sk-ant-test-key"
        assert "sk-ant-test-key" not in json.dumps(client.payloads[0])

    @pytest.mark.anyio
    async def test_the_request_carries_the_allowlist_and_the_system_prompt(
        self, world, monkeypatch
    ):
        from app.ai.prompts import SYSTEM_PROMPT
        from app.ai.tools import TOOLS

        client = FakeClient([_text("ok")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        await ModelAnalyst(world, _settings()).ask("hello")
        payload = client.payloads[0]
        assert payload["system"] == SYSTEM_PROMPT
        assert {tool["name"] for tool in payload["tools"]} == set(TOOLS)


# ======================================================================================
# Failure degrades rather than breaking
# ======================================================================================


class TestAModelFailureFallsBack:
    @pytest.mark.anyio
    async def test_a_transport_failure_answers_deterministically_and_says_why(
        self, world, monkeypatch
    ):
        client = FakeClient(
            [httpx.ConnectError("no route", request=httpx.Request("POST", LEAKY))]
        )
        monkeypatch.setattr(httpx, "AsyncClient", client)
        answer = await ModelAnalyst(world, _settings()).ask(
            "show me our critical infrastructure"
        )
        assert answer.degraded_reason == "The language model is unreachable."
        # The answer is a real one, from the deterministic router.
        assert "CRITICAL entities in this workspace" in answer.answer

    @pytest.mark.anyio
    async def test_a_rejected_credential_is_reported_without_the_credential(
        self, world, monkeypatch
    ):
        client = FakeClient([FakeResponse({}, status_code=401)])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        answer = await ModelAnalyst(world, _settings()).ask("hello")
        assert answer.degraded_reason == "The configured model credential was rejected."
        assert "sk-ant" not in (answer.degraded_reason or "")
        assert "sk-ant" not in answer.answer

    @pytest.mark.anyio
    async def test_the_fallback_answer_keeps_the_selected_context(self, world, monkeypatch):
        client = FakeClient([httpx.TimeoutException("slow")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        event_id = next(iter(world._events))
        answer = await ModelAnalyst(world, _settings()).ask(
            "investigate this event", selected_event_id=event_id
        )
        assert answer.degraded_reason == "The language model timed out."
        assert answer.answer


class TestTheFailureReasonIsSafeToShow:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (httpx.TimeoutException("x"), "The language model timed out."),
            (
                httpx.ConnectError("x", request=httpx.Request("POST", LEAKY)),
                "The language model is unreachable.",
            ),
            (ValueError("x"), "The language model call failed (ValueError)."),
        ],
    )
    def test_each_shape_has_its_own_wording(self, error, expected):
        assert _describe_model_error(error) == expected

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (401, "The configured model credential was rejected."),
            (429, "The language model is rate-limited."),
            (500, "The language model returned HTTP 500."),
            (503, "The language model returned HTTP 503."),
        ],
    )
    def test_status_codes_are_distinguished(self, code, expected):
        """401 and 429 need different operator responses: fix the key, or wait."""
        request = httpx.Request("POST", LEAKY)
        error = httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(code, request=request)
        )
        assert _describe_model_error(error) == expected

    def test_no_reason_carries_the_url_or_the_key(self):
        request = httpx.Request("POST", LEAKY)
        for error in (
            httpx.ConnectError("connecting to " + LEAKY, request=request),
            httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request)
            ),
            RuntimeError("x-api-key: sk-ant-secret123"),
        ):
            reason = _describe_model_error(error)
            assert "sk-ant-secret123" not in reason
            assert "api.anthropic.com" not in reason


# ======================================================================================
# What reaches the prompt
# ======================================================================================


class TestOnlyPointersReachThePrompt:
    """Reality Pass §26: never send the inventory to the model."""

    @pytest.mark.anyio
    async def test_nothing_selected_adds_nothing(self, world):
        assert _selected_context_block(ToolContext(state=world)) == ""

    @pytest.mark.anyio
    async def test_a_selection_is_sent_as_an_id_and_nothing_else(self, world):
        entity = world.entities()[0]
        block = _selected_context_block(
            ToolContext(state=world, selected_entity_id=entity.id)
        )
        assert block == f"[operator context: selected_entity_id={entity.id}]\n\n"
        # The entity's own attributes are not in the prompt; a tool call fetches them.
        assert entity.type.value not in block
        assert str(entity.criticality.value) not in block

    @pytest.mark.anyio
    async def test_all_three_pointers_are_carried_in_order(self, world):
        block = _selected_context_block(
            ToolContext(
                state=world,
                selected_entity_id="ent-1",
                selected_event_id="evt-1",
                active_scenario_id="scn-1",
            )
        )
        assert block == (
            "[operator context: selected_entity_id=ent-1, selected_event_id=evt-1, "
            "active_scenario_id=scn-1]\n\n"
        )

    @pytest.mark.anyio
    async def test_the_estate_never_reaches_the_request_body(self, world, monkeypatch):
        """The strongest form: no entity name from the graph appears in the payload."""
        client = FakeClient([_text("ok")])
        monkeypatch.setattr(httpx, "AsyncClient", client)
        entity = world.entities()[0]
        await ModelAnalyst(world, _settings()).ask(
            "how are we?", selected_entity_id=entity.id
        )
        body = json.dumps(client.payloads[0])
        assert entity.id in body  # the pointer
        names = {e.name for e in world.entities() if len(e.name) > 8}
        assert not [name for name in names if name in body]


# ======================================================================================
# Which backend is chosen, and how it is labelled
# ======================================================================================


class TestBackendSelection:
    @pytest.mark.anyio
    async def test_a_configured_key_selects_the_model_analyst(self, world):
        analyst = build_analyst(world, _settings())
        assert isinstance(analyst, ModelAnalyst)
        assert analyst.engine == "model"

    @pytest.mark.anyio
    async def test_no_key_selects_the_deterministic_analyst(self, world):
        analyst = build_analyst(world, _settings(anthropic_api_key=None))
        assert isinstance(analyst, DeterministicAnalyst)

    def test_the_status_names_the_model_when_one_is_configured(self):
        status = analyst_status(_settings())
        assert status["engine"] == "model"
        assert status["model"] == _settings().anthropic_model
        assert status["available"] is True
        assert status["notice"] is None

    def test_the_status_says_deterministic_when_no_key_is_set(self):
        """The UI must not imply a model answered when the router did."""
        status = analyst_status(_settings(anthropic_api_key=None))
        assert status["engine"] != "model"
        assert status["notice"]
