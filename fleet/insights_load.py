"""Opt-in load profile that gives Revenium AI Insights something to find.

`python run_fleet.py --insights-load` runs these patterns. Each one deliberately produces a
kind of waste that one or more Insights investigators look for (retries, errors, cut-off
replies, oversized models, outdated models, repeated large prompts, redundant agent calls).
Every call goes through fleet/llm.py, so it is metered exactly like the normal fleet, but it
is tagged trace type `insights-load` and job type `insights_load`.

This module never prints: it reports through the on_trace / on_call / on_error callbacks.
See docs/SURFACING-INSIGHTS.md for the investigator map and notes on porting to AWS Bedrock.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from fleet import config, data, llm

TRACE_TYPE = "insights-load"
JOB_TYPE = "insights_load"
MAX_ROUNDS = 10
HOT_CUSTOMER = "globex"  # gets most of the traffic: the subscriber-concentration signal
# Acme is deliberately absent: it only appears in normal fleet runs (see the dormant-spend recipe).
BASELINE_CUSTOMERS = ("globex", "initech", "umbrella")

TRIAGE_SYSTEM = (
    "You triage customer support tickets. Reply with one JSON object only, no prose: "
    '{"category": "billing" | "technical" | "account" | "compliance" | "how-to", '
    '"priority": "low" | "normal" | "high" | "urgent"}.'
)
REPLY_SYSTEM = (
    "You are a senior support agent writing the reply the customer will read. Never promise "
    "refunds, credits, or legal or compliance commitments. Reply with the message text only."
)


class Trace:
    """One pattern run for one customer: a single Revenium trace (and agentic job)."""

    def __init__(self, run_id: str, round_no: int, pattern: "Pattern", customer_id: str,
                 on_call: Callable, on_error: Callable):
        self.run_id = run_id
        self.round_no = round_no
        self.pattern = pattern
        self.customer = data.CUSTOMERS[customer_id]
        self.tickets = [t for t in data.TICKETS if t.customer_id == customer_id]
        self.trace_id = f"{run_id}-r{round_no}-{pattern.key}-{customer_id}"
        self.attempted = 0  # every attempted call sends one record, failed or not
        self.calls = []  # llm.LLMResult, in call order
        self.expected_errors = 0
        self._on_call = on_call
        self._on_error = on_error

    def call(self, agent: str, task_type: str, transaction_name: str, system: str, prompt: str, *,
             parent: Optional[str] = None, retry_number: int = 0, model: Optional[str] = None,
             max_tokens: Optional[int] = None, expect_error: bool = False) -> Optional[llm.LLMResult]:
        """One metered call in this trace. Returns None only for a failure the pattern expects."""
        requester = self.tickets[0]  # the hot subscriber for Globex: marta.kessler
        metadata = llm.build_usage_metadata(
            agent=agent,
            task_type=task_type,
            trace_id=self.trace_id,
            trace_name=f"{self.customer.org} – insights load: {self.pattern.label}",
            transaction_name=transaction_name,
            organization_name=self.customer.org,
            subscription_id=self.customer.subscription_id,
            subscriber_id=requester.requester_id,
            subscriber_email=requester.requester_email,
            agentic_job_id=self.trace_id,
            agentic_job_name=f"Insights load: {self.pattern.label}",
            agentic_job_type=JOB_TYPE,
            parent_transaction_id=parent,
            retry_number=retry_number,
            trace_type=TRACE_TYPE,
        )
        self.attempted += 1
        try:
            result = llm.complete(agent, system, prompt, metadata, model=model, max_tokens=max_tokens)
        except Exception as exc:  # KeyboardInterrupt is not an Exception, so Ctrl-C still propagates
            provider, configured = config.AGENT_MODELS[agent]
            reason = f"{type(exc).__name__}: {exc}"
            self._on_error(self.pattern.key, agent, provider, model or configured, reason, expect_error)
            if expect_error:
                self.expected_errors += 1
                return None
            raise
        self.calls.append(result)
        self._on_call(self.pattern.key, result)
        return result


@dataclass(frozen=True)
class Pattern:
    key: str
    label: str
    investigators: tuple  # Revenium AI Insights investigator ids this pattern feeds
    calls: int  # LLM calls per trace
    run: Callable[[Trace], None]
    customers: tuple = (HOT_CUSTOMER,)
    expected_errors: int = 0  # deliberate failures per trace


@dataclass
class PatternRun:
    round_no: int
    pattern_key: str
    label: str
    investigators: tuple
    customer_id: str
    org: str
    attempted: int
    calls: list = field(default_factory=list)
    expected_errors: int = 0
    error: Optional[str] = None


def _ticket_text(ticket) -> str:
    return f"Ticket: {ticket.id}\nSubject: {ticket.subject}\n\n{ticket.body}"


# --- Patterns ------------------------------------------------------------------------------
# Each pattern is one small function. Its header names the investigators it feeds, what the
# waste looks like in Revenium, and how to port it to Bedrock.


# Retry waste (retry-waste): the same reply is drafted three times in one trace.
# In Revenium: three responder calls with one parent and retry_number 0, 1, 2.
# Bedrock: same metadata and loop; only the client and model id change.
def retry_waste(trace: Trace) -> None:
    ticket = trace.tickets[0]
    root = trace.call("classifier", "ticket-classification", "Classify ticket",
                      TRIAGE_SYSTEM, _ticket_text(ticket))
    prompt = f"Write the reply to this ticket in at most 120 words.\n\n{_ticket_text(ticket)}"
    # A simulated strict validator rejects the first two drafts, so the identical request is
    # sent again. Every attempt hangs off the classifier, as retries of one step do.
    for attempt in range(3):
        trace.call("responder", "response-drafting", "Draft reply", REPLY_SYSTEM, prompt,
                   parent=root.response_id, retry_number=attempt)


PATTERNS = (
    Pattern("retry-waste", "Retry waste", ("retry-waste",), 4, retry_waste),
)

CALLS_PER_ROUND = sum(p.calls * len(p.customers) for p in PATTERNS)
EXPECTED_ERRORS_PER_ROUND = sum(p.expected_errors * len(p.customers) for p in PATTERNS)


def estimated_calls(rounds: int) -> int:
    """Exact number of LLM calls `rounds` rounds make, deliberate failures included."""
    return rounds * CALLS_PER_ROUND


def hot_calls_per_round() -> int:
    return sum(p.calls for p in PATTERNS for c in p.customers if c == HOT_CUSTOMER)


def preflight_rows(rounds: int) -> list:
    """(label, value) rows the CLI prints before any paid call."""
    return [
        ("Mode", f"insights-load (trace type {TRACE_TYPE}, job type {JOB_TYPE})"),
        ("Rounds", f"{rounds} (max {MAX_ROUNDS})"),
        ("Estimated calls", f"{estimated_calls(rounds)} LLM calls ({CALLS_PER_ROUND} per round, "
                            f"{EXPECTED_ERRORS_PER_ROUND} per round fail on purpose)"),
        ("Hot customer", f"{data.CUSTOMERS[HOT_CUSTOMER].org} "
                         f"({hot_calls_per_round()} of {CALLS_PER_ROUND} calls per round)"),
    ]


def run_load(run_id: str, rounds: int, results: list, on_trace: Callable, on_call: Callable,
             on_error: Callable) -> None:
    """Run every pattern `rounds` times; append one PatternRun per finished trace to `results`.

    on_trace(round_no, pattern, customer); on_call(pattern_key, result);
    on_error(pattern_key, agent, provider, model, reason, expected).
    """
    if not 1 <= rounds <= MAX_ROUNDS:  # checked again here, before any call
        raise ValueError(f"rounds must be between 1 and {MAX_ROUNDS}, got {rounds}")
    for round_no in range(1, rounds + 1):
        for pattern in PATTERNS:
            for customer_id in pattern.customers:
                on_trace(round_no, pattern, data.CUSTOMERS[customer_id])
                trace = Trace(run_id, round_no, pattern, customer_id, on_call, on_error)
                error = None
                try:
                    pattern.run(trace)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                # Appended as soon as the trace ends, so a Ctrl-C keeps every finished trace.
                results.append(PatternRun(
                    round_no, pattern.key, pattern.label, pattern.investigators, customer_id,
                    trace.customer.org, trace.attempted, trace.calls, trace.expected_errors, error,
                ))
