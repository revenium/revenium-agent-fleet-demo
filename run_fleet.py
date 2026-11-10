import sys

# Keep this file parseable by Python 3.9 so this message can print (no `match`, no `X | Y`).
if sys.version_info < (3, 10):
    sys.exit(
        f"This demo needs Python 3.10+ (you are on {sys.version.split()[0]}). "
        "Create a venv: python3.12 -m venv .venv && source .venv/bin/activate"
    )

import argparse
import os
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

OUTCOMES = ("DEFLECTED", "ESCALATED", "FAILED")
REQUIRED_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "REVENIUM_METERING_API_KEY")

console = Console(soft_wrap=True)


def make_run_id() -> str:
    return f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def mask(key: str) -> str:
    """First 4 and last 4 characters only; short keys are hidden completely."""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"


def _flag(name: str) -> bool:
    # Same truthy rule the Revenium SDK uses for its boolean env flags.
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def circuit_breaker_status() -> str:
    if not _flag("REVENIUM_CIRCUIT_BREAKER_ENABLED"):
        return "off"
    team = os.environ.get("REVENIUM_TEAM_ID", "").strip()
    if not team:
        return "ENABLED BUT REVENIUM_TEAM_ID MISSING - enforcement will not poll"
    raw = os.environ.get("REVENIUM_CB_POLL_INTERVAL_SECONDS", "").strip()
    # Same rule as the SDK: a positive whole number, otherwise its 60-second default.
    poll = f"{raw}s" if raw.isdigit() and int(raw) > 0 else ("60s (invalid value ignored)" if raw else "60s")
    return f"on (team {mask(team)}, poll {poll})"


def outcomes_status() -> str:
    """Preflight value for ticket outcomes; reads the env only and never shows the full key."""
    key = os.environ.get("REVENIUM_WRITE_API_KEY", "").strip()
    if not key:
        return "OFF - REVENIUM_WRITE_API_KEY is empty in .env, so no outcomes will be reported"
    if not key.startswith("rev_sk_"):
        return f"WARNING - write key {mask(key)} does not start with rev_sk_; outcome reporting will fail"
    # The SDK sends outcomes to REVENIUM_PROFITSTREAM_BASE_URL when it is set, so show that address.
    other = os.environ.get("REVENIUM_PROFITSTREAM_BASE_URL", "").strip()
    return f"on (write key {mask(key)}{f', sent to {other}' if other else ''})"


DEFAULT_BASE_URL = "https://api.revenium.ai"


def _base_url() -> str:
    """The address the SDK will actually meter to: it ignores a value that is not an http(s) URL."""
    raw = os.environ.get("REVENIUM_METERING_BASE_URL", "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return f"{DEFAULT_BASE_URL} (INVALID REVENIUM_METERING_BASE_URL {raw!r} IGNORED)"
    return raw


def print_preflight(run_id: str, config) -> None:
    """Everything the run will use, shown before any paid call. Never prints a full key."""
    console.print(f"Run ID: {run_id}", highlight=False)
    console.rule("Preflight")
    rows = [
        ("Revenium key", mask(os.environ.get("REVENIUM_METERING_API_KEY", "").strip())),
        ("Base URL", _base_url()),
        ("Environment", config.ENVIRONMENT),
        ("Circuit breaker", circuit_breaker_status()),
        ("Prompt capture", "on" if _flag("REVENIUM_CAPTURE_PROMPTS") else "off"),
        ("Outcomes", outcomes_status()),
    ] + [(agent, f"{provider}/{model}") for agent, (provider, model) in config.AGENT_MODELS.items()]
    for label, value in rows:
        # Plain lines, not a rich Table: tables wrap long values and break grep.
        console.print(f"{label:<16} {value}", markup=False, highlight=False)


def print_call(result) -> None:
    """One line per successful LLM call: tokens and latency, never dollars."""
    console.print(
        f"✓ {result.agent} {result.provider}/{result.model} "
        f"in={result.input_tokens} out={result.output_tokens} {result.latency_s:.2f}s",
        markup=False,
        highlight=False,
    )
    if result.stop_reason == "max_tokens":
        console.print(f"[yellow]Warning: {result.agent} hit max_tokens; the reply may be cut off[/yellow]")


def print_tool(tool_id: str, detail: str) -> None:
    """One indented line per metered tool lookup."""
    console.print(f"  tool {tool_id} → {detail}", markup=False, highlight=False)


def run_check(run_id: str, console: Console) -> list:
    """One metered call per configured agent model, chained into a single trace.

    Returns (agent, "<ExcType>: <message>") for each provider call that failed.
    """
    from fleet import config, llm

    failures = []
    parent_id = None  # always the last successful call, so one failure does not break the chain
    for agent, (provider, model) in config.AGENT_MODELS.items():
        metadata = llm.build_usage_metadata(
            agent=agent,
            task_type="smoke-check",
            trace_id=f"{run_id}-check",
            trace_name="Smoke check",
            transaction_name=f"Check {agent} ({model})",
            organization_name="Demo – Smoke Check",
            subscription_id="demo-smoke-check",
            subscriber_id="presenter",
            subscriber_email="presenter@demo.example",
            agentic_job_id=f"check-{run_id}",
            agentic_job_name="Smoke check",
            agentic_job_type="smoke_check",
            parent_transaction_id=parent_id,
            retry_number=0,
        )
        try:
            result = llm.complete(
                agent,
                "You are a connectivity check. Answer tersely.",
                "Reply with the single word: ok",
                metadata,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failures.append((agent, reason))
            console.print(f"✗ {agent} {provider}/{model} {reason}", style="red", markup=False, highlight=False)
            continue
        parent_id = result.response_id
        print_call(result)
    return failures


def run_batch(run_id: str, tickets, results: list) -> None:
    """Run tickets one after another; append each TicketResult to the caller's list as it finishes."""
    from fleet import agents, config, data

    for ticket in tickets:
        org = data.customer(ticket).org
        console.print(f"━━ {ticket.id} · {org} · {ticket.subject}", style="bold", markup=False, highlight=False)
        result = agents.run_ticket(
            run_id,
            ticket,
            lambda _ticket, call: print_call(call),
            lambda _ticket, tool_id, detail: print_tool(tool_id, detail),
        )
        results.append(result)
        if result.outcome == "FAILED":
            provider, model = config.AGENT_MODELS[result.failed_agent]
            console.print(
                f"✗ {result.failed_agent} {provider}/{model} {result.error}",
                style="red",
                markup=False,
                highlight=False,
            )
            console.print(
                f"{ticket.id} failed at {result.failed_agent}; continuing",
                style="yellow",
                markup=False,
                highlight=False,
            )
            continue
        score = f"{result.score:.2f}" if result.score is not None else "n/a"
        console.print(
            f"→ {ticket.id} {result.outcome} · {result.category}/{result.priority} · QA {result.verdict} {score}",
            style="green" if result.outcome == "DEFLECTED" else "yellow",
            markup=False,
            highlight=False,
        )


def print_summary(results: list, run_id: str) -> None:
    """Calls and tokens per agent and per customer, outcome counts, and where to look in Revenium.

    Tokens only: Revenium prices every call, so the terminal never shows a local dollar figure.
    """
    from fleet import config, data

    console.rule("Summary")
    calls = [call for r in results for call in r.calls]

    agents_table = Table(title="Calls and tokens per agent", box=box.SIMPLE)
    agents_table.add_column("Agent", no_wrap=True)
    for name in ("Calls", "Input tok", "Output tok"):
        agents_table.add_column(name, justify="right")
    for agent in config.AGENT_MODELS:
        mine = [c for c in calls if c.agent == agent]
        agents_table.add_row(
            Text(agent),
            Text(str(len(mine))),
            Text(str(sum(c.input_tokens for c in mine))),
            Text(str(sum(c.output_tokens for c in mine))),
        )
    console.print(agents_table)

    customers_table = Table(title="Calls and tokens per customer", box=box.SIMPLE)
    customers_table.add_column("Customer", no_wrap=True)
    for name in ("Tickets", "Calls", "Tokens"):
        customers_table.add_column(name, justify="right")
    customers_table.add_column("Outcomes")
    for customer_id, customer in data.CUSTOMERS.items():
        mine = [r for r in results if r.ticket.customer_id == customer_id]
        if not mine:
            continue
        mine_calls = [c for r in mine for c in r.calls]
        outcomes = []
        for outcome in OUTCOMES:
            n = sum(1 for r in mine if r.outcome == outcome)
            if n:
                outcomes.append(f"{outcome} {n}")
        customers_table.add_row(
            Text(customer.org),
            Text(str(len(mine))),
            Text(str(len(mine_calls))),
            Text(str(sum(c.input_tokens + c.output_tokens for c in mine_calls))),
            Text(", ".join(outcomes)),
        )
    console.print(customers_table)

    tokens_in = sum(c.input_tokens for c in calls)
    tokens_out = sum(c.output_tokens for c in calls)
    tool_calls = sum(len(r.tools) for r in results)
    console.print(
        f"Totals: {len(results)} tickets · {len(calls)} LLM calls · in={tokens_in} out={tokens_out}"
        f" · {tool_calls} tool calls",
        markup=False,
        highlight=False,
    )
    counts = " · ".join(f"{o} {sum(1 for r in results if r.outcome == o)}" for o in OUTCOMES)
    console.print(f"Outcomes: {counts}", markup=False, highlight=False)
    for line in (
        "Where to look in Revenium (data lands in about a minute):",
        f'  Traces       trace type "{config.TRACE_TYPE}", search {run_id}   one call tree per ticket',
        f"  Agents       group cost by agent: {', '.join(config.AGENT_MODELS)}",
        '  Customers    organizations named "Demo – …" (Globex should cost the most)',
        "  Tools        kb-search and crm-lookup tool events, linked to each ticket's job",
        '  Quality      response quality score on the qa-reviewer "QA note" call (QA makes 2 calls per ticket)',
        "  Outcomes     jobs of type support_ticket with DEFLECTED / ESCALATED (only with REVENIUM_WRITE_API_KEY)",
    ):
        console.print(line, markup=False, highlight=False)


def report_outcomes(results: list) -> None:
    """Report each ticket's outcome on its Revenium job, in ticket order; stop at the first failure.

    Runs after the metering flush. Sends only the category and QA score as metadata, never
    ticket or model text. A failure here never changes the exit code.
    """
    from fleet import llm

    if not llm.outcomes_enabled():
        console.print(
            "Outcomes skipped: set REVENIUM_WRITE_API_KEY (rev_sk_…) to report DEFLECTED/ESCALATED",
            style="yellow",
            markup=False,
            highlight=False,
        )
        return
    reported = 0
    for i, r in enumerate(results):
        metadata = {"category": r.category}
        if r.score is not None:
            metadata["qa_score"] = r.score
        try:
            llm.report_outcome(r.job_id, r.outcome, reason=r.error, metadata=metadata)
        except Exception as exc:
            console.print(
                f"Outcome reporting failed for {r.job_id} ({type(exc).__name__}: {exc}); "
                f"skipping the remaining {len(results) - i - 1}",
                style="yellow",
                markup=False,
                highlight=False,
            )
            break
        reported += 1
    console.print(f"Outcomes reported: {reported} of {len(results)} tickets", markup=False, highlight=False)


def print_delivery(status, run_id: str) -> None:
    """The delivery line. It must stay the last line of the run."""
    if status.last_error:
        console.print(status.last_error, markup=False, highlight=False)
    counts = f"sent={status.sent} failed={status.failed} unsent={status.unsent} (run {run_id})"
    if status.healthy:
        console.print(f"[green]Metering OK: {counts}[/green]", highlight=False)
    else:
        console.print(f"[red]Metering FAILED: {counts}[/red]", highlight=False)


def run_fleet(run_id: str, limit) -> int:
    """Fleet mode: every ticket (or the first `limit`) through the four agents."""
    from fleet import data, llm

    tickets = data.TICKETS[:limit] if limit else data.TICKETS
    console.print(f"{'Tickets':<16} {len(tickets)} of {len(data.TICKETS)}", markup=False, highlight=False)

    results = []
    interrupted = False
    completed = False
    try:
        run_batch(run_id, tickets, results)
        completed = True
    except KeyboardInterrupt:
        interrupted = True
        console.print("[yellow]Interrupted - flushing metering before exit[/yellow]")
    finally:
        # One record per LLM call and tool event, plus the ERROR record of each failed call.
        expected = sum(len(r.calls) + len(r.tools) + (r.outcome == "FAILED") for r in results)
        status = llm.flush_and_status(expected, 15.0)
        # Outcomes go after the flush: Revenium creates each job from its first ingested record.
        if completed:
            report_outcomes(results)
        else:
            console.print("[yellow]Outcomes skipped: run interrupted[/yellow]")
        print_summary(results, run_id)  # on Ctrl-C: the tickets finished so far
        print_delivery(status, run_id)

    if interrupted:
        return 130
    failed = any(r.outcome == "FAILED" for r in results)
    return 1 if failed or not status.healthy else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Revenium agent fleet demo")
    parser.add_argument(
        "--check",
        action="store_true",
        help="make one real metered call per configured agent model and confirm Revenium delivery",
    )
    parser.add_argument(
        "--tickets",
        type=int,
        default=None,
        metavar="N",
        help="fleet mode: process only the first N tickets (default: all)",
    )
    args = parser.parse_args(argv)
    if args.tickets is not None and args.tickets < 1:
        parser.error("--tickets must be 1 or more")
    # No flag runs the fleet; --check runs the smoke check and ignores --tickets.

    # .env must win over shell exports, and must load BEFORE the Revenium SDK is imported.
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    # Every run builds its own call trees: a parent id inherited from the shell would graft
    # the first call onto a foreign trace.
    # The SDK also stamps these on every call when set; neither belongs on a support ticket.
    for name in ("REVENIUM_PARENT_TRANSACTION_ID", "REVENIUM_TICKET_ID", "REVENIUM_AGENTIC_JOB_VERSION"):
        os.environ.pop(name, None)

    missing = [k for k in REQUIRED_KEYS if not os.environ.get(k, "").strip()]
    if missing:
        console.print(f"[red]Missing {', '.join(missing)} - add to .env (see .env.example)[/red]")
        return 1

    from fleet import agents, config, data, llm  # noqa: F401  (fleet.llm loads the Revenium SDK, which patches both providers)

    if not llm.metering_ready():
        key = mask(os.environ.get("REVENIUM_METERING_API_KEY", "").strip())
        console.print(
            f"REVENIUM_METERING_API_KEY {key} (base URL {_base_url()}) was rejected by the Revenium SDK: "
            "it must start with hak_ or rev_. No paid calls were made.",
            style="red",
            markup=False,
            highlight=False,
        )
        return 1

    run_id = make_run_id()
    print_preflight(run_id, config)

    llm.register_error_printer(
        lambda msg: console.print(msg, style="bold red", markup=False, highlight=False)
    )

    if not args.check:
        return run_fleet(run_id, args.tickets)

    failures = []
    attempted = 0
    interrupted = False
    try:
        failures = run_check(run_id, console)
        attempted = len(config.AGENT_MODELS)  # every call, failed or not, sends one record
    except KeyboardInterrupt:
        interrupted = True
        console.print("[yellow]Interrupted - flushing metering before exit[/yellow]")
    finally:
        if failures:
            names = ", ".join(agent for agent, _ in failures)
            console.print(
                f"{len(failures)} of {len(config.AGENT_MODELS)} provider calls failed: {names}",
                style="red",
                markup=False,
                highlight=False,
            )
        status = llm.flush_and_status(attempted, 15.0)
        if status.last_error:
            console.print(status.last_error, markup=False, highlight=False)
        counts = f"sent={status.sent} failed={status.failed} unsent={status.unsent} (run {run_id})"
        if status.healthy:
            console.print(f"[green]Metering OK: {counts}[/green]", highlight=False)
        else:
            console.print(f"[red]Metering FAILED: {counts}[/red]", highlight=False)

    # Provider health and metering health are separate signals: a failed provider call
    # still produces a metering record, so the delivery counters alone cannot reveal it.
    if interrupted:
        return 130
    return 1 if failures or not status.healthy else 0


if __name__ == "__main__":
    sys.exit(main())
