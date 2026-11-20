"""Seed a Revenium tenant with synthetic metering records that surface AI Insights.

Revenium AI Insights investigators only report a finding once a pattern reaches a minimum
volume and dollar amount. A quiet non-production tenant never gets there, and real LLM calls
cannot get there affordably. This script posts synthetic, backdated completion records straight
to the Revenium metering API instead. It makes no LLM calls and needs no provider keys: only the
Revenium metering key the demo already uses.

  python seed_insights.py              # DRY RUN (default): builds and summarises, sends nothing
  python seed_insights.py --fire       # sends the records (asks you to type yes first)
  python seed_insights.py --verify     # starts an Insights run and reports what it found

Every record is tagged so it stays identifiable: environment and trace type `insights-seed`,
product `Insights Seed`, organization and trace names starting `Insights Seed –`, and a run tag
(`seed-YYYYMMDD-xxxx`) on agent names, error reasons and names.

The spend is synthetic but it is real data in the tenant: it appears in cost dashboards,
customer and model reports, and this tool cannot delete it. Use a sandbox tenant.
See docs/SURFACING-INSIGHTS.md.
"""
import sys

# Keep this file parseable by Python 3.9 so this message can print (no `match`, no `X | Y`).
if sys.version_info < (3, 10):
    sys.exit(
        f"This demo needs Python 3.10+ (you are on {sys.version.split()[0]}). "
        "Create a venv: python3.12 -m venv .venv && source .venv/bin/activate"
    )

import argparse
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from dotenv import load_dotenv
from rich.console import Console

# Only the key-masking rule and the default address; run_fleet never imports the SDK at import time.
from run_fleet import DEFAULT_BASE_URL, mask

TRACE_TYPE = "insights-seed"
PRODUCT_NAME = "Insights Seed"
ORG_PREFIX = "Insights Seed –"
DEFAULT_ENVIRONMENT = "insights-seed"
REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
PER_CALL_DAYS = 7  # per-call scenarios are stamped inside the last 7 days
CURRENT_DAYS = 30  # "current" window of the time-series scenarios
BASELINE_DAYS = 28  # baseline window that precedes it
DORMANT_PRIOR_DAYS = 60  # how far before the current window dormant activity is placed
DEFAULT_TPS = 10.0
MAX_TPS = 50.0
WORKERS = 8
DEFAULT_PERIOD_DAYS = 30
POLL_SECONDS = 15
VERIFY_TIMEOUT_SECONDS = 1200
INGEST_WAIT_SECONDS = 90
DEFAULT_APP_URL = "https://app.revenium.ai"
MAX_BODY = 200  # never print more than this much of a response body
PROGRESS_EVERY = 500
MAX_FAILURES_SHOWN = 5

# Estimate only: list price in USD per token as (input, output). Revenium computes the real cost.
PRICES = {
    "claude-sonnet-4-6": (3e-6, 1.5e-5),
    "claude-3-5-sonnet-latest": (3e-6, 1.5e-5),
    "gpt-4o": (2.5e-6, 1e-5),
    "gpt-5.6-terra": (2.5e-6, 1.5e-5),
}

TAG_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
ENV_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FINISHED = ("completed", "complete", "succeeded", "success", "partial")
FAILED = ("failed", "error", "cancelled", "canceled")
ACTIVE = ("queued", "pending", "running")

console = Console(soft_wrap=True)


def say(text: str, style=None) -> None:
    """Plain output: no rich markup, so API-derived text is never interpreted."""
    console.print(text, style=style, markup=False, highlight=False)


# --------------------------------------------------------------------------- records


def make_tag() -> str:
    return f"seed-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:4]}"


@dataclass
class SeedContext:
    tag: str
    environment: str
    now: datetime  # timezone-aware UTC, fixed when the context is created


def _hex(n: int) -> str:
    return uuid.uuid4().hex[:n]


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def build_record(
    ctx, scenario, *, ts, model, provider, agent, task_type, inp, out, org,
    stop_reason="END", error_reason=None, operation_type="CHAT", retry_number=None,
    cache_read=None, cache_create=None, parent_span_id=None, trace_id=None, span_id=None,
    credential=None, extra=None,
) -> dict:
    """One synthetic completion record in the metering API's wire format (camelCase)."""
    if stop_reason == "ERROR" and not error_reason:
        raise ValueError("an ERROR record needs an error_reason")
    if stop_reason != "ERROR" and error_reason is not None:
        raise ValueError("error_reason is only allowed on ERROR records")
    ttft = random.randint(120, 400)
    duration = random.randint(1500, 6000)
    subscriber = {"id": f"{scenario}-{ctx.tag}", "email": f"{scenario}@insights-seed.example"}
    if credential:
        # A synthetic label, never a key.
        subscriber["credential"] = {"name": credential, "value": f"{credential}-{ctx.tag}"}
    rec = {
        "spanId": span_id or f"span_{_hex(16)}",
        "traceId": trace_id or f"trace_{_hex(16)}",
        "model": model,
        "provider": provider,
        "costType": "AI",
        "stopReason": stop_reason,
        "isStreamed": True,
        "inputTokenCount": inp,
        "outputTokenCount": out,
        "reasoningTokenCount": 0,
        "totalTokenCount": inp + out,
        "requestTime": _iso(ts),
        "timeToFirstToken": ttft,
        "requestDuration": duration,
        "completionStartTime": _iso(ts + timedelta(milliseconds=ttft)),
        "responseTime": _iso(ts + timedelta(milliseconds=duration)),
        "taskType": task_type,
        "transactionName": scenario,
        "operationType": operation_type,
        "agent": f"{agent}-{ctx.tag}",
        "environment": ctx.environment,
        "region": random.choice(REGIONS),
        "traceType": TRACE_TYPE,
        "traceName": f"{ORG_PREFIX} {scenario} ({ctx.tag})",
        "subscriber": subscriber,
        "organizationName": f"{ORG_PREFIX} {org} ({ctx.tag})",
        "productName": PRODUCT_NAME,
    }
    optional = {
        "retryNumber": retry_number,
        "cacheReadTokenCount": cache_read,
        "cacheCreationTokenCount": cache_create,
        "parentSpanId": parent_span_id,
    }
    rec.update({k: v for k, v in optional.items() if v is not None})
    if stop_reason == "ERROR":
        rec["errorReason"] = f"{error_reason}-{ctx.tag}"
    if extra:
        rec.update(extra)
    return rec


def estimate_cost(record: dict) -> float:
    """List-price estimate in USD. Revenium computes the real cost from the model string."""
    p_in, p_out = PRICES.get(record["model"], (0.0, 0.0))
    return record["inputTokenCount"] * p_in + record["outputTokenCount"] * p_out


# --------------------------------------------------------------------------- time placement

_SAFETY = timedelta(seconds=7)  # longest requestDuration is 6 s, so responseTime stays <= now


def _latest(ctx) -> datetime:
    return ctx.now - _SAFETY


def _clamp(ctx, ts, lo=None, hi=None) -> datetime:
    hi = min(hi or _latest(ctx), _latest(ctx))
    if lo is not None and ts < lo:
        ts = lo
    return min(ts, hi)


def _between(ctx, start_days_ago, end_days_ago) -> datetime:
    """Uniform random time between start_days_ago and end_days_ago (start > end), before now − 7 s."""
    lo = ctx.now - timedelta(days=start_days_ago)
    hi = min(ctx.now - timedelta(days=end_days_ago), _latest(ctx))
    if end_days_ago > 0:
        hi = hi - _SAFETY  # strictly older than the window edge
    return lo + (hi - lo) * random.random()


def _last_days(ctx, days) -> datetime:
    return _between(ctx, days, 0)


# --------------------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    key: str
    investigator: str  # public investigator id shown in the preflight and the docs
    aliases: tuple  # category names a finding for this scenario may carry
    builder: Callable
    note: str = ""
    per_call: bool = False  # stamped within the last PER_CALL_DAYS


SONNET = ("claude-sonnet-4-6", "anthropic")
TERRA = ("gpt-5.6-terra", "openai")


def build_retry_waste(ctx) -> list:
    """retry-waste: needs >=10 retries above a 5% retry rate, with retry rows >= $50.

    15 failed first attempts (small) and 25 large retries (retryNumber 1, 2, 3) on the same
    agent; the retries alone cost about $52 at list price.
    """
    key = "retry-waste"
    model, provider = SONNET
    common = dict(model=model, provider=provider, agent="enrichment-agent",
                  task_type="data_enrichment", org="retry waste", stop_reason="ERROR",
                  error_reason="timeout", operation_type="TOOL_CALL")
    records, chains = [], []
    for _ in range(15):
        ts = _between(ctx, PER_CALL_DAYS, 0.05)  # leave room for the retries that follow
        trace = f"trace_{_hex(16)}"
        chains.append((trace, ts))
        records.append(build_record(ctx, key, ts=ts, inp=4_000, out=1_500, retry_number=0,
                                    trace_id=trace, **common))
    for j in range(25):
        trace, first = chains[j % 15]
        retry = j % 3 + 1
        ts = _clamp(ctx, first + timedelta(seconds=30 * retry + j))
        records.append(build_record(ctx, key, ts=ts, inp=300_000, out=80_000, retry_number=retry,
                                    trace_id=trace, **common))
    return records


def build_max_tokens_cutoff(ctx) -> list:
    """max-tokens-cutoff-v1: a cluster of truncated answers (80 TOKEN_LIMIT of 120 rows, about $96)."""
    key = "max-tokens-cutoff"
    model, provider = TERRA
    records = []
    for i in range(120):
        records.append(build_record(
            ctx, key, ts=_last_days(ctx, CURRENT_DAYS), model=model, provider=provider,
            agent="long-answer-agent", task_type="long_form_answer", inp=200_000, out=20_000,
            org="max tokens cutoff", stop_reason="TOKEN_LIMIT" if i < 80 else "END",
        ))
    return records


SCENARIOS = {
    s.key: s
    for s in (
        Scenario(
            "retry-waste", "retry-waste", ("retry_waste",), build_retry_waste,
            note="If it is missing from a 30-day run, verify with --period-days 7.",
            per_call=True,
        ),
        Scenario(
            "max-tokens-cutoff", "max-tokens-cutoff-v1", ("truncation_cluster", "max_tokens_cutoff"),
            build_max_tokens_cutoff,
            note="May not fire: the metering API takes the TOKEN_LIMIT stop reason, and this "
            "investigator may only recognise provider-native stop reasons.",
        ),
    )
}


def build(ctx, keys) -> dict:
    """Records per selected scenario, in SCENARIOS order."""
    return {k: SCENARIOS[k].builder(ctx) for k in SCENARIOS if k in keys}


# --------------------------------------------------------------------------- network


_client = None
_client_lock = threading.Lock()


def http_call(method, url, headers, body=None):
    """The only network function. Returns (status_code, response text); (0, error name) on failure.

    Never logs or returns the request headers (they carry the key).
    """
    global _client
    import httpx  # lazy: the dry run and the tests never import it

    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=20)  # thread-safe; shared by the send workers
    try:
        response = _client.request(method, url, headers=headers, json=body)
        return response.status_code, response.text
    except httpx.HTTPError as exc:
        return 0, type(exc).__name__


def send(records, api_key, meter_url, tps):
    """Post every record once (no retries) at no more than `tps` request starts per second.

    Returns (ok, failed, interrupted).
    """
    headers = {"x-api-key": api_key, "content-type": "application/json", "accept": "application/json"}
    interval = 1.0 / tps
    gate = threading.Lock()
    next_start = [time.monotonic()]
    stop = threading.Event()

    def post(record):
        with gate:  # start-rate limiter: each start is >= interval after the previous one
            wait = next_start[0] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            next_start[0] = max(time.monotonic(), next_start[0]) + interval
        if stop.is_set():
            return None
        return http_call("POST", meter_url, headers, record)

    total = len(records)
    ok = failed = done = 0
    failures = []
    interrupted = False
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {pool.submit(post, r): r for r in records}
    try:
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            status, body = result
            done += 1
            if 200 <= status < 300:
                ok += 1
            else:
                failed += 1
                if len(failures) < MAX_FAILURES_SHOWN:
                    failures.append((futures[future]["transactionName"], status, body[:MAX_BODY]))
            if done % PROGRESS_EVERY == 0 and done < total:
                say(f"  sent {done}/{total} (ok={ok} failed={failed})")
    except KeyboardInterrupt:
        interrupted = True
        stop.set()
        for future in futures:
            future.cancel()
    finally:
        pool.shutdown(wait=not interrupted, cancel_futures=True)
    say(f"  sent {done}/{total} (ok={ok} failed={failed})")
    for key, status, body in failures:
        say(f"  failed: {key} HTTP {status} {body}", style="red")
    return ok, failed, interrupted


# --------------------------------------------------------------------------- verify


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _unwrap(obj):
    """Run payloads may be wrapped in {"run": {...}} or {"data": {...}}."""
    if isinstance(obj, dict) and "status" not in obj and "state" not in obj:
        for key in ("run", "data"):
            if isinstance(obj.get(key), dict):
                return obj[key]
    return obj if isinstance(obj, dict) else {}


def _run_id(obj):
    for candidate in (obj, *(obj.get(k) for k in ("run", "data") if isinstance(obj, dict))):
        if isinstance(candidate, dict):
            for key in ("id", "runId", "run_id"):
                if candidate.get(key):
                    return str(candidate[key])
    return None


def _active_run(app_url, headers):
    status, text = http_call("GET", f"{app_url}/api/v2/insights/runs", headers)
    if not 200 <= status < 300:
        say(f"Could not list Insights runs: HTTP {status} {text[:MAX_BODY]}", style="red")
        return None
    obj = _json(text)
    items = obj.get("data") if isinstance(obj, dict) else obj
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("status", "")).lower() in ACTIVE:
            rid = item.get("runId") or item.get("id")
            if rid:
                return str(rid)
    return None


def _status(run) -> str:
    return str(run.get("status") or run.get("state") or "").lower()


def _findings(run) -> list:
    for key in ("parsedFindings", "findings", "findingsJson"):
        value = run.get(key)
        if isinstance(value, str):
            value = _json(value)
        if isinstance(value, list):
            return [f for f in value if isinstance(f, dict)]
    return []


def normalize(category: str) -> str:
    return re.sub(r"_v\d+$", "", str(category).lower().replace("-", "_"))


def _categories(finding) -> list:
    return [finding[k] for k in ("category", "investigatorId", "investigator") if finding.get(k)]


def verify(app_url, api_key, period_days, keys, tag) -> int:
    """Start an Insights run (or follow the active one), wait for it and report per scenario.

    Returns 0 when the run completes, even if some scenarios produced no finding.
    `tag` is the seed tag to look for in the findings, or None.
    """
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    now = datetime.now(timezone.utc)
    zulu = lambda ts: ts.isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    body = {
        "periodStart": zulu(now - timedelta(days=period_days, minutes=2)),
        "periodEnd": zulu(now + timedelta(minutes=1)),
    }
    say(f"Starting an Insights run over the last {period_days} days at {app_url}")
    status, text = http_call("POST", f"{app_url}/api/v2/insights/runs", headers, body)
    run_id = None
    if status in (200, 201, 202):
        run_id = _run_id(_json(text)) or _active_run(app_url, headers)
    elif status == 409:
        say("An Insights run is already active in this tenant; following it. "
            "Its period may differ from the one requested.", style="yellow")
        run_id = _active_run(app_url, headers)
    elif status in (401, 403):
        say(f"The Insights API rejected key {mask(api_key)} (HTTP {status}). Set "
            "REVENIUM_INSIGHTS_API_KEY in .env to a key that can start Insights runs.", style="red")
        return 1
    else:
        say(f"Could not start an Insights run: HTTP {status} {text[:MAX_BODY]}", style="red")
        return 1
    if not run_id:
        say("Could not find the Insights run id in the response.", style="red")
        return 1

    say(f"Insights run {run_id}: waiting for it to finish (polling every {POLL_SECONDS} s)")
    deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
    last = None
    while True:
        status, text = http_call("GET", f"{app_url}/api/v2/insights/runs/{run_id}", headers)
        run = {}
        if 200 <= status < 300:
            run = _unwrap(_json(text))
            state = _status(run)
            if state != last:
                say(f"  status: {state or 'unknown'}")
                last = state
            if state in FINISHED:
                break
            if state in FAILED:
                say(f"Insights run {run_id} ended with status {state}.", style="red")
                return 1
        elif status in (401, 403):
            say(f"The Insights API rejected key {mask(api_key)} while polling (HTTP {status}).", style="red")
            return 1
        else:
            say(f"  poll: HTTP {status} {text[:MAX_BODY]}")
        if time.monotonic() > deadline:
            say(f"Timed out after {VERIFY_TIMEOUT_SECONDS} s. Check run {run_id} in the Revenium app.",
                style="red")
            return 1
        time.sleep(POLL_SECONDS)

    findings = _findings(run)
    counts = {}
    for finding in findings:
        label = str((_categories(finding) or ["unknown"])[0])
        counts[label] = counts.get(label, 0) + 1
    say("Findings by category:")
    if not counts:
        say("  (none)")
    for label, n in sorted(counts.items()):
        extra = ""
        if tag and any(tag in json.dumps(f) for f in findings if label in _categories(f)):
            extra = "  (mentions seed tag)"
        say(f"  {label}: {n}{extra}")

    found_cats = {normalize(c) for f in findings for c in _categories(f)}
    say("Seeded scenarios:")
    hits = 0
    for key in keys:
        scenario = SCENARIOS[key]
        if found_cats & {normalize(a) for a in scenario.aliases}:
            hits += 1
            say(f"  FOUND      {key} ({scenario.investigator})", style="green")
            continue
        say(f"  not found  {key} ({scenario.investigator})", style="yellow")
        if scenario.note:
            say(f"             {scenario.note}")
        if scenario.per_call and period_days > PER_CALL_DAYS:
            say(f"             Per-call scenario: try --verify --period-days {PER_CALL_DAYS}.")
    say(f"Insights verify: {hits} of {len(keys)} seeded scenarios produced findings")
    say("Not found does not mean the seed failed: Revenium does not publish the exact thresholds.")
    return 0


# --------------------------------------------------------------------------- CLI


def _metering_base():
    """(base, error). Never falls back silently to production on an invalid value."""
    raw = os.environ.get("REVENIUM_METERING_BASE_URL", "").strip()
    if not raw:
        return DEFAULT_BASE_URL, None
    base = raw.rstrip("/")
    if base.endswith("/meter"):
        base = base[: -len("/meter")]
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return base, f"REVENIUM_METERING_BASE_URL {raw!r} is not an http(s) URL"
    return base, None


def _money(value: float) -> str:
    return f"~${value:,.2f}"


def print_plan(ctx, mode, key, meter_url, selected) -> tuple:
    """Preflight and one line per scenario. Returns (total records, estimated $)."""
    rows = [
        ("Seed tag", ctx.tag),
        ("Mode", mode),
        ("Revenium key", mask(key) if key else "not set"),
        ("Metering URL", meter_url),
        ("Environment", ctx.environment),
        ("Trace type", TRACE_TYPE),
        ("Product", PRODUCT_NAME),
    ]
    for label, value in rows:
        say(f"{label:<16} {value}")
    say("")
    total_records, total_cost = 0, 0.0
    for key, records in selected.items():
        scenario = SCENARIOS[key]
        times = sorted(r["requestTime"] for r in records)
        span = f"{times[0][:10]} → {times[-1][:10]}"
        spend = sum(estimate_cost(r) for r in records)
        total_records += len(records)
        total_cost += spend
        say(f"{key:<24} {len(records):>5} records  {span}  {_money(spend):>11}  {scenario.investigator}")
        if scenario.note:
            say(f"  {scenario.note}")
    return total_records, total_cost


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a Revenium tenant with synthetic, backdated metering records that surface "
        "AI Insights. The default is a DRY RUN that sends nothing. No LLM calls are made. "
        "See docs/SURFACING-INSIGHTS.md.",
    )
    parser.add_argument("--fire", action="store_true",
                        help="send the records (synthetic spend appears in the tenant and cannot be deleted)")
    parser.add_argument("--yes", action="store_true", help="with --fire: skip the interactive confirmation")
    parser.add_argument("--verify", action="store_true",
                        help="start a Revenium AI Insights run and report which scenarios produced findings")
    parser.add_argument("--detectors", default=None, metavar="KEYS",
                        help=f"comma list of scenarios (default: all): {', '.join(SCENARIOS)}")
    parser.add_argument("--tps", type=float, default=DEFAULT_TPS, metavar="N",
                        help=f"records sent per second (default {DEFAULT_TPS:g}, max {MAX_TPS:g})")
    parser.add_argument("--period-days", type=int, default=DEFAULT_PERIOD_DAYS, metavar="N",
                        help=f"--verify: Insights run period in days (1-90, default {DEFAULT_PERIOD_DAYS})")
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT,
                        help=f"environment label on every record (default {DEFAULT_ENVIRONMENT})")
    parser.add_argument("--tag", default=None,
                        help="run tag on agents, error reasons and names (default seed-YYYYMMDD-xxxx)")
    args = parser.parse_args(argv)

    # Validate everything before .env loads.
    if args.yes and not args.fire:
        parser.error("--yes only works with --fire")
    if args.detectors is None:
        keys = list(SCENARIOS)
    else:
        wanted = [k.strip() for k in args.detectors.split(",") if k.strip()]
        unknown = [k for k in wanted if k not in SCENARIOS]
        if unknown or not wanted:
            parser.error(f"unknown --detectors {', '.join(unknown) or '(empty)'}; "
                         f"valid keys: {', '.join(SCENARIOS)}")
        keys = [k for k in SCENARIOS if k in wanted]
    if not 0 < args.tps <= MAX_TPS:
        parser.error(f"--tps must be above 0 and at most {MAX_TPS:g}")
    if not 1 <= args.period_days <= 90:
        parser.error("--period-days must be between 1 and 90")
    if not ENV_RE.match(args.environment):
        parser.error("--environment may only use letters, digits, - and _ (max 64)")
    if args.tag is not None and not TAG_RE.match(args.tag):
        parser.error("--tag may only use letters, digits and - (max 40)")

    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    try:
        return _run(args, keys)
    except KeyboardInterrupt:
        say("Interrupted.", style="red")
        return 130


def _run(args, keys) -> int:
    key = os.environ.get("REVENIUM_METERING_API_KEY", "").strip()
    base, base_error = _metering_base()
    meter_url = f"{base}/meter/v2/ai/completions"
    tag = args.tag or make_tag()
    mode = "FIRE" if args.fire else ("VERIFY" if args.verify else "DRY RUN - nothing is sent")

    ctx = SeedContext(tag=tag, environment=args.environment, now=datetime.now(timezone.utc))
    selected = build(ctx, keys)
    records, spend = print_plan(ctx, mode, key, meter_url, selected)
    minutes = records / args.tps / 60
    say(f"{'TOTAL':<24} {records:>5} records  {_money(spend)}  about {minutes:.0f} min to send "
        f"at {args.tps:g} records/s")
    say("Estimates use list prices; Revenium computes the real cost.")

    if base_error:
        if not (args.fire or args.verify):
            say(f"WARNING: {base_error}; --fire and --verify will refuse to run.", style="yellow")
        else:
            say(f"{base_error}. Nothing was sent. Fix it in .env (or leave it empty for "
                f"{DEFAULT_BASE_URL}).", style="red")
            return 1

    if not (args.fire or args.verify):
        say("Dry run: nothing was sent. Add --fire to send these records.")
        return 0

    if args.fire:
        if not key.startswith(("hak_", "rev_")):
            say("REVENIUM_METERING_API_KEY must be set in .env to a metering key starting hak_ or "
                "rev_. Nothing was sent.", style="red")
            return 1
        say("")
        say(f"WARNING: this sends {records} records with {_money(spend)} of SYNTHETIC spend to key "
            f"{mask(key)} at {meter_url}.", style="bold yellow")
        say("The spend appears in this tenant's cost dashboards, customer and model reports. "
            "This tool cannot delete it. Use a sandbox tenant, never production.", style="yellow")
        minute = datetime.now(timezone.utc).minute
        if "fan-out" in keys and (minute >= 50 or minute < 5):
            say("WARNING: the fan-out rows must arrive within one UTC hour; this close to the hour "
                "boundary they may land in different hours. Consider waiting a few minutes.",
                style="yellow")
        if not args.yes:
            if not sys.stdin.isatty():
                say("--yes is required in non-interactive shells. Nothing was sent.", style="red")
                return 1
            try:
                answer = input(f"Type yes to send {records} synthetic records: ")
            except EOFError:
                answer = ""
            if answer.strip() != "yes":
                say("Not confirmed. Nothing was sent.", style="red")
                return 1
        # Rebuild with a fresh clock so the time-sensitive rows (fan-out) are current.
        fresh = SeedContext(tag=tag, environment=args.environment, now=datetime.now(timezone.utc))
        ordered = [r for recs in build(fresh, keys).values() for r in recs]
        say(f"Sending {len(ordered)} records at {args.tps:g}/s (tag {tag})")
        ok, failed, interrupted = send(ordered, key, meter_url, args.tps)
        say(f"Seed delivery: ok={ok} failed={failed} (tag {tag})", style="green" if not failed else "red")
        if interrupted:
            return 130
        if failed:
            return 1
        if not args.verify:
            say(f"Next: python seed_insights.py --verify --tag {tag}")
            return 0
        say(f"Waiting {INGEST_WAIT_SECONDS} s for Revenium to ingest the records before the Insights run.")
        time.sleep(INGEST_WAIT_SECONDS)

    verify_key = (os.environ.get("REVENIUM_INSIGHTS_API_KEY", "").strip() or key)
    if not verify_key:
        say("Set REVENIUM_INSIGHTS_API_KEY or REVENIUM_METERING_API_KEY in .env to run --verify.",
            style="red")
        return 1
    app_url = os.environ.get("REVENIUM_INSIGHTS_APP_URL", "").strip().rstrip("/")
    if not app_url:
        if base != DEFAULT_BASE_URL:
            say(f"The metering address is {base}, so the Revenium app address is unknown. Set "
                "REVENIUM_INSIGHTS_APP_URL in .env.", style="red")
            return 1
        app_url = DEFAULT_APP_URL
    parts = urlsplit(app_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        say(f"REVENIUM_INSIGHTS_APP_URL {app_url!r} is not an http(s) URL.", style="red")
        return 1
    known_tag = tag if (args.tag or args.fire) else None
    return verify(app_url, verify_key, args.period_days, keys, known_tag)


if __name__ == "__main__":
    sys.exit(main())
