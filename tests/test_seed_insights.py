"""Offline tests for the synthetic Insights seeder (seed_insights.py).

Run from the repository root: python -m unittest -v tests.test_seed_insights

No network call is ever made: seed_insights.http_call is replaced by a fake in every test,
seed_insights.load_dotenv is a no-op so .env is never loaded, and the Revenium settings are
set or blanked per test.
"""
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from rich.console import Console

import seed_insights as si
from run_fleet import mask

FAKE_KEY = "hak_offline_test_key_1234"
NOW = datetime(2026, 11, 20, 12, 30, 0, tzinfo=timezone.utc)
TAG = "seed-test"


def ctx():
    return si.SeedContext(tag=TAG, environment="insights-seed", now=NOW)


def parse(ts):
    return datetime.fromisoformat(ts)


def cost(records):
    return sum(si.estimate_cost(r) for r in records)


def no_network(*_args, **_kwargs):
    raise AssertionError("http_call must not run in this test")


class Base(unittest.TestCase):
    env = {}

    def setUp(self):
        base_env = {
            "REVENIUM_METERING_API_KEY": "",
            "REVENIUM_METERING_BASE_URL": "",
            "REVENIUM_INSIGHTS_APP_URL": "",
            "REVENIUM_INSIGHTS_API_KEY": "",
        }
        base_env.update(self.env)
        self.out = io.StringIO()
        self.calls = []
        self.responder = no_network
        for patcher in (
            mock.patch.dict(os.environ, base_env),
            mock.patch.object(si, "load_dotenv", lambda *a, **k: None),
            mock.patch.object(si, "http_call", self._http),
            mock.patch.object(si, "POLL_SECONDS", 0),
            mock.patch.object(si, "INGEST_WAIT_SECONDS", 0),
            mock.patch.object(si, "console", Console(file=self.out, soft_wrap=True, width=200)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _http(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        return self.responder(method, url, headers, body)

    @property
    def text(self):
        return self.out.getvalue()


class BuildRecordTests(unittest.TestCase):
    def test_wire_fields(self):
        c = ctx()
        rec = si.build_record(
            c, "retry-waste", ts=NOW - timedelta(hours=1), model="claude-sonnet-4-6",
            provider="anthropic", agent="a", task_type="t", inp=100, out=20, org="o",
        )
        self.assertEqual(rec["costType"], "AI")
        self.assertEqual(rec["totalTokenCount"], 120)
        self.assertEqual(rec["environment"], "insights-seed")
        self.assertEqual(rec["traceType"], "insights-seed")
        self.assertEqual(rec["productName"], "Insights Seed")
        self.assertTrue(rec["organizationName"].startswith("Insights Seed –"))
        self.assertIn(TAG, rec["organizationName"])
        self.assertTrue(rec["traceName"].startswith("Insights Seed –"))
        self.assertIn(TAG, rec["traceName"])
        self.assertTrue(rec["agent"].endswith(TAG))
        self.assertNotIn("errorReason", rec)
        self.assertLessEqual(parse(rec["requestTime"]), parse(rec["completionStartTime"]))
        self.assertLessEqual(parse(rec["completionStartTime"]), parse(rec["responseTime"]))
        self.assertLessEqual(parse(rec["responseTime"]), NOW)

    def test_error_reason_rule(self):
        c = ctx()
        common = dict(ts=NOW - timedelta(hours=1), model="m", provider="p", agent="a",
                      task_type="t", inp=1, out=1, org="o")
        with self.assertRaises(ValueError):
            si.build_record(c, "k", stop_reason="ERROR", **common)
        with self.assertRaises(ValueError):
            si.build_record(c, "k", error_reason="boom", **common)
        rec = si.build_record(c, "k", stop_reason="ERROR", error_reason="boom", **common)
        self.assertTrue(rec["errorReason"].endswith(TAG))

    def test_credential_is_a_label(self):
        rec = si.build_record(
            ctx(), "k", ts=NOW - timedelta(hours=1), model="m", provider="p", agent="a",
            task_type="t", inp=1, out=1, org="o", credential="cred",
        )
        self.assertEqual(rec["subscriber"]["credential"], {"name": "cred", "value": f"cred-{TAG}"})


class RetryWasteTests(unittest.TestCase):
    def test_retry_waste_shape(self):
        records = si.build(ctx(), ["retry-waste"])["retry-waste"]
        self.assertEqual(len(records), 40)
        retries = [r for r in records if r.get("retryNumber", 0) > 0]
        self.assertGreaterEqual(len(retries), 10)
        self.assertGreater(len(retries) / len(records), 0.05)
        self.assertTrue(all(r["stopReason"] == "ERROR" for r in records))
        self.assertTrue(all(r["operationType"] == "TOOL_CALL" for r in records))
        self.assertGreaterEqual(cost(retries), 50)


class DryRunTests(Base):
    def test_dry_run_sends_nothing_without_key(self):
        self.assertEqual(si.main([]), 0)
        self.assertIn("DRY RUN", self.text)
        self.assertEqual(self.calls, [])

    def test_dry_run_single_scenario(self):
        self.assertEqual(si.main(["--detectors", "retry-waste"]), 0)
        self.assertIn("DRY RUN", self.text)
        self.assertTrue(any(line.startswith("retry-waste ") for line in self.text.splitlines()))
        self.assertEqual(self.calls, [])

    def test_unknown_detector_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            si.main(["--detectors", "nope"])
        self.assertEqual(cm.exception.code, 2)

    def test_yes_needs_fire(self):
        with self.assertRaises(SystemExit) as cm:
            si.main(["--yes"])
        self.assertEqual(cm.exception.code, 2)


class DryRunWithKeyTests(Base):
    env = {"REVENIUM_METERING_API_KEY": FAKE_KEY}

    def test_key_is_masked(self):
        self.assertEqual(si.main(["--detectors", "retry-waste"]), 0)
        self.assertIn(mask(FAKE_KEY), self.text)
        self.assertNotIn(FAKE_KEY, self.text)
        self.assertEqual(self.calls, [])


class FireRefusalTests(Base):
    def test_fire_without_key(self):
        self.assertEqual(si.main(["--fire", "--yes", "--detectors", "retry-waste"]), 1)
        self.assertIn("REVENIUM_METERING_API_KEY", self.text)
        self.assertEqual(self.calls, [])


class FireTests(Base):
    env = {"REVENIUM_METERING_API_KEY": FAKE_KEY}

    def test_fire_needs_confirmation_when_not_a_tty(self):
        with mock.patch.object(si.sys.stdin, "isatty", return_value=False):
            self.assertEqual(si.main(["--fire", "--detectors", "retry-waste"]), 1)
        self.assertIn("--yes", self.text)
        self.assertEqual(self.calls, [])

    def test_invalid_base_url_stops_fire(self):
        with mock.patch.dict(os.environ, {"REVENIUM_METERING_BASE_URL": "not a url"}):
            self.assertEqual(si.main(["--fire", "--yes", "--detectors", "retry-waste"]), 1)
        self.assertEqual(self.calls, [])

    def test_fire_posts_each_record_once(self):
        self.responder = lambda *a: (201, "")
        rc = si.main(["--fire", "--yes", "--detectors", "retry-waste", "--tps", "50"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 40)
        for method, url, headers, body in self.calls:
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/meter/v2/ai/completions"))
            self.assertEqual(headers["x-api-key"], FAKE_KEY)
            self.assertIsInstance(body, dict)
        self.assertEqual(len({c[3]["spanId"] for c in self.calls}), 40)
        self.assertIn("failed=0", self.text)
        self.assertNotIn(FAKE_KEY, self.text)

    def test_fire_reports_a_failure(self):
        state = {"n": 0}

        def responder(*_a):
            state["n"] += 1
            return (500, "boom") if state["n"] == 7 else (201, "")

        self.responder = responder
        rc = si.main(["--fire", "--yes", "--detectors", "retry-waste", "--tps", "50"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls), 40)  # no retries
        self.assertIn("failed=1", self.text)

    def test_meter_suffix_is_normalized(self):
        self.responder = lambda *a: (201, "")
        with mock.patch.dict(os.environ, {"REVENIUM_METERING_BASE_URL": "https://example.test/meter/"}):
            si.main(["--fire", "--yes", "--detectors", "retry-waste", "--tps", "50"])
        self.assertEqual(self.calls[0][1], "https://example.test/meter/v2/ai/completions")


class VerifyTests(Base):
    env = {"REVENIUM_METERING_API_KEY": FAKE_KEY}

    def test_verify_reports_found_and_not_found(self):
        def responder(method, url, headers, body):
            if method == "POST":
                self.assertTrue(url.endswith("/api/v2/insights/runs"))
                self.assertEqual(headers["authorization"], f"Bearer {FAKE_KEY}")
                self.assertIn("periodStart", body)
                self.assertIn("periodEnd", body)
                return 201, '{"id":"run-1"}'
            self.assertTrue(url.endswith("/api/v2/insights/runs/run-1"))
            return 200, json.dumps({"status": "COMPLETED", "findings": [{"category": "retry_waste"}]})

        self.responder = responder
        rc = si.main(["--verify", "--detectors", "retry-waste,max-tokens-cutoff"])
        self.assertEqual(rc, 0)
        lines = self.text.splitlines()
        self.assertTrue(any("retry-waste" in l and "FOUND" in l for l in lines))
        self.assertTrue(any("max-tokens-cutoff" in l and "not found" in l for l in lines))
        self.assertIn(si.SCENARIOS["max-tokens-cutoff"].note, self.text)
        self.assertTrue(self.calls[0][1].startswith(si.DEFAULT_APP_URL))
        self.assertNotIn(FAKE_KEY, self.text)

    def test_verify_follows_active_run_on_409(self):
        def responder(method, url, headers, body):
            if method == "POST":
                return 409, '{"message":"already running"}'
            if url.endswith("/api/v2/insights/runs"):
                return 200, json.dumps({"data": [
                    {"runId": "run-0", "status": "COMPLETED"},
                    {"runId": "run-2", "status": "running"},
                ]})
            self.assertTrue(url.endswith("/runs/run-2"))
            return 200, json.dumps({"status": "completed", "parsedFindings": []})

        self.responder = responder
        self.assertEqual(si.main(["--verify", "--detectors", "retry-waste"]), 0)
        self.assertIn("run-2", self.text)

    def test_verify_rejected_key(self):
        self.responder = lambda *a: (401, "unauthorized")
        self.assertEqual(si.main(["--verify", "--detectors", "retry-waste"]), 1)
        self.assertIn("REVENIUM_INSIGHTS_API_KEY", self.text)
        self.assertNotIn(FAKE_KEY, self.text)

    def test_verify_needs_app_url_for_custom_base(self):
        with mock.patch.dict(os.environ, {"REVENIUM_METERING_BASE_URL": "https://example.test"}):
            self.assertEqual(si.main(["--verify", "--detectors", "retry-waste"]), 1)
        self.assertIn("REVENIUM_INSIGHTS_APP_URL", self.text)
        self.assertEqual(self.calls, [])


class VerifyWithoutKeyTests(Base):
    def test_verify_without_any_key(self):
        self.assertEqual(si.main(["--verify", "--detectors", "retry-waste"]), 1)
        self.assertEqual(self.calls, [])


ALL_KEYS = (
    "fan-out error-concentration outdated-model prompt-cache-miss retry-waste model-task-fit "
    "model-awareness customer-concentration credential-spike max-tokens-cutoff dormant-spend "
    "spend-trajectory unit-economics-drift"
).split()
EXPECTED_COUNTS = {
    "fan-out": 102, "error-concentration": 600, "outdated-model": 1000, "prompt-cache-miss": 750,
    "retry-waste": 40, "model-task-fit": 320, "model-awareness": 140, "customer-concentration": 330,
    "credential-spike": 1170, "max-tokens-cutoff": 120, "dormant-spend": 260,
    "spend-trajectory": 884, "unit-economics-drift": 700,
}


class AllScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = si.build(ctx(), list(si.SCENARIOS))
        cls.all = [r for recs in cls.built.values() for r in recs]

    def recs(self, key):
        return self.built[key]

    def age(self, rec):
        return NOW - parse(rec["requestTime"])

    # whole build

    def test_registry_order_and_counts(self):
        self.assertEqual(list(si.SCENARIOS), ALL_KEYS)
        self.assertEqual(list(self.built), ALL_KEYS)
        self.assertEqual({k: len(v) for k, v in self.built.items()}, EXPECTED_COUNTS)
        self.assertEqual(len(self.all), 6416)

    def test_wire_fields_everywhere(self):
        for r in self.all:
            self.assertEqual(r["costType"], "AI")
            self.assertEqual(r["totalTokenCount"], r["inputTokenCount"] + r["outputTokenCount"])
            self.assertIn(r["stopReason"], ("END", "ERROR", "TOKEN_LIMIT"))
            self.assertIn(r["operationType"], ("CHAT", "TOOL_CALL"))
            self.assertIn(r["region"], si.REGIONS)
            self.assertEqual(r["environment"], "insights-seed")
            self.assertEqual(r["traceType"], "insights-seed")
            self.assertEqual(r["productName"], "Insights Seed")
            self.assertTrue(r["organizationName"].startswith("Insights Seed –"))
            self.assertIn(TAG, r["organizationName"])
            self.assertTrue(r["traceName"].startswith("Insights Seed –"))
            self.assertTrue(r["agent"].endswith(TAG))
            self.assertIn(r["model"], si.PRICES)
            self.assertEqual("errorReason" in r, r["stopReason"] == "ERROR")
            if "errorReason" in r:
                self.assertTrue(r["errorReason"].endswith(TAG))

    def test_timestamps(self):
        floor = NOW - timedelta(days=91)
        for r in self.all:
            req, first, resp = (parse(r[k]) for k in ("requestTime", "completionStartTime", "responseTime"))
            self.assertTrue(floor <= req <= first <= resp <= NOW, r["transactionName"])

    def test_per_call_windows(self):
        week = timedelta(days=7)
        for key, scenario in si.SCENARIOS.items():
            if not scenario.per_call:
                continue
            for r in self.recs(key):
                if key == "prompt-cache-miss" and r["inputTokenCount"] == 10_000:
                    self.assertTrue(week < self.age(r) <= timedelta(days=14))
                else:
                    self.assertLessEqual(self.age(r), week, key)
        per_call = {k for k, s in si.SCENARIOS.items() if s.per_call}
        self.assertEqual(per_call, {"error-concentration", "outdated-model", "prompt-cache-miss",
                                    "retry-waste", "model-task-fit", "model-awareness"})

    def test_fan_out_is_sent_first(self):
        self.assertEqual(next(iter(si.SCENARIOS)), "fan-out")

    # per scenario

    def test_fan_out(self):
        recs = self.recs("fan-out")
        by_trace = {}
        for r in recs:
            by_trace.setdefault(r["traceId"], []).append(r)
        depth = [r for r in recs if "-depth-" in r["agent"] or r["agent"].startswith("depth-")]
        self.assertEqual(len(depth), 12)
        self.assertEqual(len({r["traceId"] for r in depth}), 1)
        for prev, cur in zip(depth, depth[1:]):
            self.assertEqual(cur["parentSpanId"], prev["spanId"])
        for r in depth:
            self.assertTrue(timedelta(hours=2) <= self.age(r) <= timedelta(hours=5))
        parents = [r for r in recs if r["agent"].startswith("fan-src-")]
        self.assertEqual(len(parents), 12)
        sizes = []
        for parent in parents:
            children = [r for r in by_trace[parent["traceId"]] if r["agent"].startswith("fan-tgt-")]
            self.assertTrue(all(c["parentSpanId"] == parent["spanId"] for c in children))
            sizes.append(len(children))
        self.assertEqual(sorted(sizes), [3] * 9 + [15] * 3)
        cycle = [r for r in recs if r["agent"].startswith("cycle-")]
        self.assertEqual(len(cycle), 6)
        self.assertEqual(len({r["agent"] for r in cycle}), 2)
        for prev, cur in zip(cycle, cycle[1:]):
            self.assertEqual(cur["parentSpanId"], prev["spanId"])
            self.assertNotEqual(cur["agent"], prev["agent"])
        for r in parents + cycle + [r for r in recs if r["agent"].startswith("fan-tgt-")]:
            self.assertLessEqual(self.age(r), timedelta(seconds=120))

    def test_error_concentration(self):
        recs = self.recs("error-concentration")
        self.assertEqual(len(recs), 600)
        self.assertTrue(all(r["stopReason"] == "ERROR" for r in recs))
        reasons = {r["errorReason"] for r in recs}
        self.assertEqual(len(reasons), 1)
        self.assertIn(TAG, reasons.pop())
        agents = {}
        for r in recs:
            agents[r["agent"]] = agents.get(r["agent"], 0) + 1
        self.assertGreaterEqual(max(agents.values()) / len(recs), 0.75)

    def test_outdated_model(self):
        recs = [r for r in self.recs("outdated-model") if r["model"] == "gpt-4o"]
        self.assertGreaterEqual(len(recs), 200)
        self.assertGreaterEqual(sum(r["inputTokenCount"] for r in recs), 120_000_000)

    def test_prompt_cache_miss(self):
        recs = self.recs("prompt-cache-miss")
        current = [r for r in recs if self.age(r) <= timedelta(days=7)]
        prior = [r for r in recs if self.age(r) > timedelta(days=7)]
        self.assertGreaterEqual(len(current), 120)
        self.assertGreaterEqual(len(prior), 120)
        self.assertTrue(all(r["cacheReadTokenCount"] == 0 and r["cacheCreationTokenCount"] == 0 for r in recs))
        self.assertGreaterEqual(sum(r["inputTokenCount"] for r in current), 74_000_000)

    def test_model_task_fit(self):
        recs = self.recs("model-task-fit")
        self.assertEqual(len({(r["agent"], r["taskType"]) for r in recs}), 1)
        rates = {}
        for model in ("claude-sonnet-4-6", "gpt-4o"):
            rows = [r for r in recs if r["model"] == model]
            self.assertGreaterEqual(len(rows), 150)
            bad = [r for r in rows if r["stopReason"] == "ERROR" or r.get("retryNumber", 0) > 0]
            rates[model] = len(bad) / len(rows)
        self.assertLessEqual(rates["claude-sonnet-4-6"], 0.05)
        self.assertGreaterEqual(rates["gpt-4o"], 0.25)

    def test_model_awareness(self):
        recs = [r for r in self.recs("model-awareness") if r["model"] == "claude-3-5-sonnet-latest"]
        self.assertGreaterEqual(len(recs), 120)

    def test_customer_concentration(self):
        recs = self.recs("customer-concentration")
        spend = {}
        for r in recs:
            spend[r["organizationName"]] = spend.get(r["organizationName"], 0) + si.estimate_cost(r)
        self.assertEqual(len(spend), 12)
        self.assertGreaterEqual(max(spend.values()) / sum(spend.values()), 0.70)

    def test_credential_spike(self):
        recs = self.recs("credential-spike")
        self.assertEqual(len({r["subscriber"]["credential"]["name"] for r in recs}), 1)
        self.assertEqual(len({r["subscriber"]["id"] for r in recs}), 1)
        baseline = [r for r in recs if self.age(r) > timedelta(days=30)]
        spike = [r for r in recs if self.age(r) <= timedelta(days=30)]
        self.assertGreater(len(spike) / 30, 3 * len(baseline) / 30)
        self.assertEqual(len(baseline), 210)

    def test_max_tokens_cutoff(self):
        recs = self.recs("max-tokens-cutoff")
        cut = [r for r in recs if r["stopReason"] == "TOKEN_LIMIT"]
        self.assertGreaterEqual(len(cut), 50)
        self.assertGreater(len(cut) / len(recs), 0.05)

    def test_dormant_spend(self):
        recs = self.recs("dormant-spend")
        agent = [r for r in recs if "dormant agent" in r["organizationName"]]
        customer = [r for r in recs if "dormant customer" in r["organizationName"]]
        self.assertEqual((len(agent), len(customer)), (120, 140))
        self.assertGreaterEqual(cost(agent), 25)
        self.assertTrue(all(self.age(r) > timedelta(days=30) for r in customer))
        self.assertTrue(all(self.age(r) <= timedelta(days=30) for r in agent))

    def _windows(self, key):
        recs = self.recs(key)
        baseline = [r for r in recs if self.age(r) > timedelta(days=30)]
        current = [r for r in recs if self.age(r) <= timedelta(days=30)]
        for r in recs:
            self.assertTrue(r["agenticJobId"].startswith(key + "-"))
            self.assertIn("agenticJobType", r)
        self.assertEqual(len({r["agenticJobId"] for r in recs}), len(recs))
        return baseline, current

    def test_spend_trajectory(self):
        baseline, current = self._windows("spend-trajectory")
        self.assertEqual((len(baseline), len(current)), (224, 660))
        self.assertGreaterEqual(cost(current), 2 * cost(baseline))
        ratio = (cost(current) / len(current)) / (cost(baseline) / len(baseline))
        self.assertTrue(0.75 <= ratio <= 1.25)

    def test_unit_economics_drift(self):
        baseline, current = self._windows("unit-economics-drift")
        self.assertEqual((len(baseline), len(current)), (280, 420))
        ratio = (cost(current) / len(current)) / (cost(baseline) / len(baseline))
        self.assertGreaterEqual(ratio, 5)


class FullDryRunTests(Base):
    def test_full_dry_run_lists_every_scenario(self):
        self.assertEqual(si.main([]), 0)
        lines = self.text.splitlines()
        for key in ALL_KEYS:
            self.assertEqual(sum(l.startswith(key + " ") for l in lines), 1, key)
        self.assertTrue(any(l.startswith("TOTAL ") and "6416" in l for l in lines))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
