"""Offline tests for the Insights load profile.

Run from the repository root: python -m unittest -v tests.test_insights_load

No paid call is ever made: both provider functions in fleet/llm.py are replaced by a fake,
the Revenium keys are blanked so no metering client starts, and .env is never loaded.
"""
import contextlib
import importlib
import io
import os
from pathlib import Path

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
        response_id = f"fake-{len(self.calls) + 1}"
        self.calls.append({
            "provider": provider, "model": model, "system": system, "prompt": prompt,
            "metadata": metadata, "max_tokens": max_tokens, "response_id": response_id,
        })
        if model == getattr(insights_load, "ERROR_MODEL", None):
            raise RuntimeError("Error code: 404 - model not found (offline fake)")
        stop = "max_tokens" if max_tokens < config.MAX_TOKENS else "end_turn"
        return "ok", response_id, 100, 10, stop

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
        root_id = calls[0]["response_id"]
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


class PatternTests(Base):
    def test_table_totals_and_order(self):
        self.assertEqual(insights_load.CALLS_PER_ROUND, 29)
        self.assertEqual(insights_load.EXPECTED_ERRORS_PER_ROUND, 2)
        self.assertEqual(
            [p.key for p in insights_load.PATTERNS],
            ["baseline", "retry-waste", "error-concentration", "max-tokens-cutoff",
             "model-task-fit", "outdated-model", "bloated-context", "fan-out"],
        )

    def test_call_counts_match_estimate(self):
        self.run_rounds(1)
        self.assertEqual(len(self.fake.calls), 29)
        self.fake.calls.clear()
        self.run_rounds(3)
        self.assertEqual(len(self.fake.calls), 87)
        self.assertEqual(insights_load.estimated_calls(3), 87)

    def test_every_call_is_tagged(self):
        self.run_rounds(1)
        for c in self.fake.calls:
            md = c["metadata"]
            self.assertEqual(md["trace_type"], "insights-load")
            self.assertEqual(md["agentic_job_type"], "insights_load")
            self.assertEqual(md["environment"], config.ENVIRONMENT)
            self.assertNotIn("ticket_id", md)

    def test_error_concentration(self):
        results = self.run_rounds(2)
        for round_no in (1, 2):
            calls = [c for c in self.calls_for("error-concentration")
                     if f"-r{round_no}-" in c["metadata"]["trace_id"]]
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(c["metadata"]["agent"] == "researcher" for c in calls))
            self.assertTrue(all(c["model"] == insights_load.ERROR_MODEL for c in calls))
            self.assertEqual([c["metadata"]["retry_number"] for c in calls], [0, 1])
            mine = [r for r in results if r.round_no == round_no]
            self.assertEqual(sum(r.expected_errors for r in mine), 2)
        self.assertTrue(all(r.error is None for r in results))

    def test_max_tokens_cutoff(self):
        self.run_rounds(1)
        calls = self.calls_for("max-tokens-cutoff")
        self.assertEqual(sorted(c["provider"] for c in calls), ["anthropic", "openai"])
        self.assertLess(insights_load.CUTOFF_MAX_TOKENS, config.MAX_TOKENS)
        self.assertTrue(all(c["max_tokens"] == insights_load.CUTOFF_MAX_TOKENS for c in calls))

    def test_model_task_fit(self):
        self.run_rounds(1)
        calls = self.calls_for("model-task-fit")
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c["metadata"]["agent"], "classifier")
            self.assertEqual(c["metadata"]["task_type"], "ticket-classification")
            self.assertEqual(c["model"], config.AGENT_MODELS["responder"][1])
        self.assertNotEqual(calls[0]["prompt"], calls[1]["prompt"])

    def test_outdated_model(self):
        self.run_rounds(1)
        first, second = self.calls_for("outdated-model")
        self.assertEqual((first["provider"], first["model"]),
                         ("anthropic", insights_load.LEGACY_ANTHROPIC_MODEL))
        self.assertEqual((second["provider"], second["model"]),
                         ("openai", insights_load.LEGACY_OPENAI_MODEL))
        self.assertEqual(second["metadata"]["parent_transaction_id"], first["response_id"])

    def test_bloated_context(self):
        self.run_rounds(1)
        calls = self.calls_for("bloated-context")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c["metadata"]["agent"] == "responder" for c in calls))
        self.assertEqual(len({c["system"] for c in calls}), 1)
        self.assertGreaterEqual(len(calls[0]["system"]), 24000)
        self.assertEqual(len({c["prompt"] for c in calls}), 3)

    def test_fan_out(self):
        self.run_rounds(1)
        calls = self.calls_for("fan-out")
        self.assertEqual(len(calls), 8)
        root = calls[0]
        self.assertEqual(root["metadata"]["agent"], "classifier")
        siblings = [c for c in calls if c["metadata"]["agent"] == "researcher"
                    and c["metadata"]["parent_transaction_id"] == root["response_id"]]
        self.assertGreaterEqual(len(siblings), 3)
        loop = calls[-4:]
        self.assertEqual([c["metadata"]["agent"] for c in loop],
                         ["qa-reviewer", "researcher", "qa-reviewer", "researcher"])
        for prev, cur in zip(calls[-5:-1], loop):
            self.assertEqual(cur["metadata"]["parent_transaction_id"], prev["response_id"])

    def test_subscriber_concentration(self):
        globex = data.CUSTOMERS["globex"].org
        for rounds in (1, 3):
            self.fake.calls.clear()
            self.run_rounds(rounds)
            orgs = [c["metadata"]["organizationName"] for c in self.fake.calls]
            self.assertGreaterEqual(orgs.count(globex) / len(orgs), 0.70)
            for c in self.fake.calls:
                md = c["metadata"]
                if md["organizationName"] != globex:
                    self.assertIn("-baseline-", md["trace_id"])
                self.assertNotEqual(md["organizationName"], data.CUSTOMERS["acme"].org)

    def test_env_overrides_legacy_model(self):
        try:
            with mock.patch.dict(os.environ, {"INSIGHTS_LEGACY_OPENAI_MODEL": "gpt-test-legacy"}):
                importlib.reload(insights_load)
                self.assertEqual(insights_load.LEGACY_OPENAI_MODEL, "gpt-test-legacy")
            with mock.patch.dict(os.environ, {"INSIGHTS_LEGACY_OPENAI_MODEL": "  "}):
                importlib.reload(insights_load)
                self.assertEqual(insights_load.LEGACY_OPENAI_MODEL,
                                 insights_load.DEFAULT_LEGACY_OPENAI_MODEL)
        finally:
            importlib.reload(insights_load)

    def test_env_example_lists_defaults(self):
        # Parses .env.example only; .env is never opened.
        path = Path(__file__).resolve().parent.parent / ".env.example"
        values = {}
        for line in path.read_text().splitlines():
            if line.startswith("INSIGHTS_") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip()
        self.assertEqual(values, {
            "INSIGHTS_ERROR_MODEL": insights_load.DEFAULT_ERROR_MODEL,
            "INSIGHTS_LEGACY_ANTHROPIC_MODEL": insights_load.DEFAULT_LEGACY_ANTHROPIC_MODEL,
            "INSIGHTS_LEGACY_OPENAI_MODEL": insights_load.DEFAULT_LEGACY_OPENAI_MODEL,
        })

    def test_preflight_rows_name_models(self):
        rows = dict(insights_load.preflight_rows(2))
        self.assertIn(f"openai/{insights_load.ERROR_MODEL}", rows["Error model"])
        self.assertIn(f"anthropic/{insights_load.LEGACY_ANTHROPIC_MODEL}", rows["Legacy models"])
        self.assertIn(f"openai/{insights_load.LEGACY_OPENAI_MODEL}", rows["Legacy models"])
        self.assertIn(f"anthropic/{config.AGENT_MODELS['responder'][1]}", rows["Model-task fit"])
        self.assertIn("58 LLM calls", rows["Estimated calls"])


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

    def test_expected_errors_print_yellow_and_unexpected_errors_exit_1(self):
        ok = lambda e, s: llm.DeliveryStatus(e, 0, 0, None, True)  # noqa: E731
        out = io.StringIO()
        with mock.patch.object(llm, "flush_and_status", ok), contextlib.redirect_stdout(out):
            self.assertEqual(run_fleet.run_insights_load("run-test", 1), 0)
        self.assertEqual(out.getvalue().count("✗ (expected)"), insights_load.EXPECTED_ERRORS_PER_ROUND)

        legacy = insights_load.LEGACY_OPENAI_MODEL
        real_openai = self.fake.openai

        def broken_openai(model, *args):
            if model == legacy:
                raise RuntimeError("Error code: 404 - unexpected (offline fake)")
            return real_openai(model, *args)

        with mock.patch.object(llm, "_openai_call", broken_openai), \
                mock.patch.object(llm, "flush_and_status", ok), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_fleet.run_insights_load("run-test", 1), 1)

    def test_help_lists_flags(self):
        code, out, _err = self._main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--insights-load", out)
        self.assertIn("--rounds", out)
        self.load_dotenv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
