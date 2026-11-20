"""Offline tests for the Insights load profile.

Run from the repository root: python -m unittest -v tests.test_insights_load

No paid call is ever made: both provider functions in fleet/llm.py are replaced by a fake,
the Revenium keys are blanked so no metering client starts, and .env is never loaded.
"""
import contextlib
import io
import os

# Set BEFORE anything imports fleet: fleet/config.py and the provider clients read env at import.
os.environ["ANTHROPIC_API_KEY"] = "offline-test-not-a-key"
os.environ["OPENAI_API_KEY"] = "offline-test-not-a-key"  # the OpenAI client needs a non-empty key
os.environ["REVENIUM_METERING_API_KEY"] = ""  # blank: no metering client starts
os.environ["REVENIUM_WRITE_API_KEY"] = ""
os.environ["REVENIUM_CIRCUIT_BREAKER_ENABLED"] = "false"
os.environ["REVENIUM_LOG_LEVEL"] = "CRITICAL"

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import run_fleet  # noqa: E402
from fleet import config, data, insights_load, llm  # noqa: E402


class FakeProvider:
    """Stands in for llm._anthropic_call / llm._openai_call and records every call."""

    def __init__(self):
        self.calls = []

    def _call(self, provider, model, system, prompt, metadata, max_tokens):
        self.calls.append({
            "provider": provider, "model": model, "system": system, "prompt": prompt,
            "metadata": metadata, "max_tokens": max_tokens,
        })
        if model == getattr(insights_load, "ERROR_MODEL", None):
            raise RuntimeError("Error code: 404 - model not found (offline fake)")
        stop = "max_tokens" if max_tokens < config.MAX_TOKENS else "end_turn"
        return "ok", f"fake-{len(self.calls)}", 100, 10, stop

    def anthropic(self, model, system, prompt, metadata, max_tokens):
        return self._call("anthropic", model, system, prompt, metadata, max_tokens)

    def openai(self, model, system, prompt, metadata, max_tokens):
        return self._call("openai", model, system, prompt, metadata, max_tokens)


def _noop(*_args, **_kwargs):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProvider()
        for target, attr, value in (
            (llm, "_anthropic_call", self.fake.anthropic),
            (llm, "_openai_call", self.fake.openai),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Any argument-validation slip reaches load_dotenv; make that fail instead of reading .env.
        self.load_dotenv = mock.Mock(side_effect=AssertionError("load_dotenv must not run in tests"))
        patcher = mock.patch.object(run_fleet, "load_dotenv", self.load_dotenv)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_rounds(self, rounds):
        results = []
        insights_load.run_load("run-test", rounds, results, _noop, _noop, _noop)
        return results

    def calls_for(self, key):
        return [c for c in self.fake.calls if f"-{key}-" in c["metadata"]["trace_id"]]


class DefaultBehaviorTests(Base):
    def _metadata(self, **extra):
        return llm.build_usage_metadata(
            agent="classifier", task_type="t", trace_id="tr", trace_name="n", transaction_name="x",
            organization_name="o", subscription_id="s", subscriber_id="i", subscriber_email="e",
            agentic_job_id="j", agentic_job_name="jn", agentic_job_type="jt", **extra,
        )

    def test_metadata_trace_type_default_unchanged(self):
        self.assertEqual(self._metadata()["trace_type"], "support-triage")
        self.assertEqual(self._metadata()["trace_type"], config.TRACE_TYPE)

    def test_metadata_trace_type_override(self):
        self.assertEqual(self._metadata(trace_type="insights-load")["trace_type"], "insights-load")

    def test_complete_without_overrides_uses_configured_model_and_max_tokens(self):
        result = llm.complete("classifier", "sys", "prompt", {})
        call = self.fake.calls[-1]
        self.assertEqual(call["model"], config.AGENT_MODELS["classifier"][1])
        self.assertEqual(call["max_tokens"], config.MAX_TOKENS)
        self.assertEqual(result.model, config.AGENT_MODELS["classifier"][1])

    def test_complete_with_overrides(self):
        result = llm.complete("classifier", "sys", "prompt", {}, model="claude-override", max_tokens=99)
        call = self.fake.calls[-1]
        self.assertEqual(call["model"], "claude-override")
        self.assertEqual(call["max_tokens"], 99)
        self.assertEqual(result.model, "claude-override")


class LoadTests(Base):
    def test_one_round_makes_calls_per_round(self):
        self.run_rounds(1)
        self.assertEqual(len(self.fake.calls), insights_load.CALLS_PER_ROUND)
        self.assertEqual(insights_load.estimated_calls(3), 3 * insights_load.CALLS_PER_ROUND)

    def test_retry_waste_shape(self):
        self.run_rounds(1)
        calls = self.calls_for("retry-waste")
        responders = [c for c in calls if c["metadata"]["agent"] == "responder"]
        self.assertEqual([c["metadata"]["retry_number"] for c in responders], [0, 1, 2])
        self.assertEqual(len({c["metadata"]["trace_id"] for c in calls}), 1)
        root_id = f"fake-{self.fake.calls.index(calls[0]) + 1}"
        self.assertEqual(calls[0]["metadata"]["agent"], "classifier")
        self.assertTrue(all(c["metadata"]["parent_transaction_id"] == root_id for c in responders))
        globex = data.CUSTOMERS["globex"].org
        for c in calls:
            self.assertEqual(c["metadata"]["trace_type"], "insights-load")
            self.assertEqual(c["metadata"]["agentic_job_type"], "insights_load")
            self.assertEqual(c["metadata"]["organizationName"], globex)

    def test_rounds_out_of_range_raise_before_any_call(self):
        for rounds in (0, insights_load.MAX_ROUNDS + 1):
            with self.assertRaises(ValueError):
                self.run_rounds(rounds)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(run_fleet.INSIGHTS_MAX_ROUNDS, insights_load.MAX_ROUNDS)


class CliTests(Base):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                run_fleet.main(argv)
        return caught.exception.code, out.getvalue(), err.getvalue()

    def assert_rejected(self, argv):
        code, _out, _err = self._main(argv)
        self.assertEqual(code, 2, argv)
        self.load_dotenv.assert_not_called()
        self.assertEqual(self.fake.calls, [])

    def test_rounds_without_insights_load(self):
        self.assert_rejected(["--rounds", "2"])

    def test_rounds_zero(self):
        self.assert_rejected(["--insights-load", "--rounds", "0"])

    def test_rounds_over_cap(self):
        self.assert_rejected(["--insights-load", "--rounds", str(run_fleet.INSIGHTS_MAX_ROUNDS + 1)])

    def test_insights_load_with_check(self):
        self.assert_rejected(["--insights-load", "--check"])

    def test_insights_load_with_tickets(self):
        self.assert_rejected(["--insights-load", "--tickets", "2"])

    def test_run_insights_load_end_to_end_offline(self):
        flushed = []

        def fake_flush(expected, timeout):
            flushed.append(expected)
            return llm.DeliveryStatus(expected, 0, 0, None, True)

        out = io.StringIO()
        with mock.patch.object(llm, "flush_and_status", fake_flush), contextlib.redirect_stdout(out):
            code = run_fleet.run_insights_load("run-test", 1)
        self.assertEqual(code, 0)
        self.assertEqual(flushed, [insights_load.CALLS_PER_ROUND])
        text = out.getvalue()
        self.assertIn(f"{insights_load.estimated_calls(1)} LLM calls", text)
        self.assertIn("Calls per pattern", text)
        self.assertTrue(text.rstrip().splitlines()[-1].startswith("Metering OK"))

    def test_help_lists_flags(self):
        code, out, _err = self._main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--insights-load", out)
        self.assertIn("--rounds", out)
        self.load_dotenv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
