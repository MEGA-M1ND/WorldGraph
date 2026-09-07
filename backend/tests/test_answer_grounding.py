"""The model may not state figures it did not get from somewhere.

The tool layer is an allowlist and it works: the model cannot execute, cannot reach the
filesystem, cannot invent an entity. But the allowlist governs what the model may *do*.
Nothing governed what it may *say*, and its final prose was returned verbatim — so a
response claiming "123,456 compromised hosts" with **zero tool calls** reached the
operator as an answer.

`SECURITY.md` claimed the model "cannot compute and cannot act". The act half was true and
enforced. The compute half was not.

The rule under test is deliberately narrow, because a narrow rule can be correct: a number
in the answer that appears neither in the operator's question nor in any tool result is a
number the model made up. It polices figures, not wording — figures are what get acted on.
"""

from __future__ import annotations

import pytest

from app.ai.grounding import check, extract_numbers, numbers_in


class TestExtraction:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("41,500 customers", [41500.0]),
            ("availability 0.9045", [0.9045]),
            ("90.45%", [90.45]),
            ("$381,563.02 per hour", [381563.02]),
            ("no figures here", []),
        ],
    )
    def test_numbers_are_read_out_of_prose(self, text, expected):
        assert extract_numbers(text) == expected

    def test_numbers_are_found_inside_nested_tool_output(self):
        payload = {"impact": {"customers": 41500, "note": "availability was 0.9045"}}
        found = numbers_in(payload)
        assert 41500.0 in found
        assert 0.9045 in found


class TestTheReportedDefect:
    def test_an_invented_figure_with_no_tool_calls_is_caught(self):
        """The reproduction, exactly."""
        result = check(
            answer="Our exposure is severe: 123,456 compromised hosts across the estate.",
            question="how bad is it?",
            tool_results=[],
        )
        assert result.is_grounded is False
        assert result.had_no_data is True
        assert "123,456" in result.ungrounded

    def test_the_explanation_names_the_figure_and_the_reason(self):
        result = check(
            answer="123,456 compromised hosts.", question="how bad is it?", tool_results=[]
        )
        described = result.describe()
        assert "123,456" in described
        assert "without calling any WorldGraph tool" in described


class TestGroundedAnswersPass:
    def test_a_figure_from_a_tool_result_is_fine(self):
        result = check(
            answer="41,500 customers are affected.",
            question="who is affected?",
            tool_results=[{"customers_affected": 41500}],
        )
        assert result.is_grounded is True

    def test_a_fraction_restated_as_a_percentage_is_fine(self):
        """0.9045 quoted as 90.45% is the same fact at a sane precision."""
        result = check(
            answer="Availability is 90.45%.",
            question="what is availability?",
            tool_results=[{"availability": 0.9045}],
        )
        assert result.is_grounded is True

    def test_a_rounded_restatement_is_fine(self):
        result = check(
            answer="Roughly 90% available.",
            question="",
            tool_results=[{"availability": 0.90451}],
        )
        assert result.is_grounded is True

    def test_a_figure_the_operator_supplied_is_fine(self):
        """Repeating the question's own numbers invents nothing."""
        result = check(
            answer="CVE-2024-21762 affects three assets.",
            question="tell me about CVE-2024-21762",
            tool_results=[],
        )
        assert result.is_grounded is True

    def test_small_numbers_are_ordinary_english(self):
        result = check(
            answer="I can help with 6 things: ...", question="what can you do?", tool_results=[]
        )
        assert result.is_grounded is True

    def test_an_answer_with_no_figures_at_all_is_grounded(self):
        result = check(
            answer="I did not recognise that. Try naming an entity.",
            question="wat",
            tool_results=[],
        )
        assert result.is_grounded is True


class TestPartialGrounding:
    def test_a_figure_absent_from_the_results_is_flagged_even_with_tool_calls(self):
        result = check(
            answer="41,500 customers and 98,765 hosts are affected.",
            question="impact?",
            tool_results=[{"customers_affected": 41500}],
        )
        assert result.is_grounded is False
        assert result.had_no_data is False
        assert result.ungrounded == ["98,765"]

    def test_the_explanation_differs_when_tools_were_called(self):
        result = check(
            answer="98,765 hosts.",
            question="impact?",
            tool_results=[{"customers_affected": 41500}],
        )
        assert "does not appear in any tool result" in result.describe()


class TestTheAnalystGate:
    """End to end through ModelAnalyst, with a stubbed model response."""

    @staticmethod
    def _analyst(world, monkeypatch, *, answer: str, outputs: list):
        from app.ai.analyst import ModelAnalyst
        from app.config import Settings

        settings = Settings(
            database_path=":memory:", anthropic_api_key="sk-ant-test-not-a-real-key"
        )
        analyst = ModelAnalyst(world, settings)

        async def fake_loop(ctx, message):
            return answer, [{"tool": "x", "ok": True}] * len(outputs), outputs

        monkeypatch.setattr(analyst, "_run_loop", fake_loop)
        return analyst

    @pytest.mark.anyio
    async def test_an_ungrounded_answer_with_no_tool_calls_is_withheld(
        self, world, monkeypatch
    ):
        analyst = self._analyst(
            world,
            monkeypatch,
            answer="Our exposure is severe: 123,456 compromised hosts.",
            outputs=[],
        )
        result = await analyst.ask("how bad is it?")

        # The fabricated figure never reaches the operator.
        assert "123,456" not in result.answer
        # And the deterministic router answered instead, which cannot fabricate.
        assert result.engine == "deterministic"
        assert result.degraded_reason
        assert "123,456" in result.degraded_reason

    @pytest.mark.anyio
    async def test_a_grounded_answer_passes_through_untouched(self, world, monkeypatch):
        analyst = self._analyst(
            world,
            monkeypatch,
            answer="41,500 customers are affected.",
            outputs=[{"customers_affected": 41500}],
        )
        result = await analyst.ask("who is affected?")
        assert result.answer == "41,500 customers are affected."
        assert result.engine == "model"

    @pytest.mark.anyio
    async def test_a_partially_ungrounded_answer_is_marked_not_dropped(
        self, world, monkeypatch
    ):
        """It may be a restatement the checker cannot recognise — but it is never verified."""
        analyst = self._analyst(
            world,
            monkeypatch,
            answer="41,500 customers and 98,765 hosts.",
            outputs=[{"customers_affected": 41500}],
        )
        result = await analyst.ask("impact?")
        assert result.engine == "model"
        assert "[UNVERIFIED]" in result.answer
        assert "98,765" in result.answer

    @pytest.mark.anyio
    async def test_a_capability_list_with_no_tools_still_answers(self, world, monkeypatch):
        """The gate must not fire on an answer that makes no quantitative claim."""
        analyst = self._analyst(
            world, monkeypatch, answer="I can trace dependencies and simulate failures.", outputs=[]
        )
        result = await analyst.ask("what can you do?")
        assert result.engine == "model"
        assert "[UNVERIFIED]" not in result.answer
