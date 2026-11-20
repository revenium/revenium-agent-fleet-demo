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

CUTOFF_MAX_TOKENS = 64  # far too small for the replies the cutoff pattern asks for

# Models the load profile calls on purpose. Override them in .env (empty falls back to these).
DEFAULT_ERROR_MODEL = "gpt-insights-load-missing-model"  # does not exist: every call fails
DEFAULT_LEGACY_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_LEGACY_OPENAI_MODEL = "gpt-4o-mini"
ERROR_MODEL = config._model("INSIGHTS_ERROR_MODEL", DEFAULT_ERROR_MODEL)
LEGACY_ANTHROPIC_MODEL = config._model("INSIGHTS_LEGACY_ANTHROPIC_MODEL", DEFAULT_LEGACY_ANTHROPIC_MODEL)
LEGACY_OPENAI_MODEL = config._model("INSIGHTS_LEGACY_OPENAI_MODEL", DEFAULT_LEGACY_OPENAI_MODEL)

TRIAGE_SYSTEM = (
    "You triage customer support tickets. Reply with one JSON object only, no prose: "
    '{"category": "billing" | "technical" | "account" | "compliance" | "how-to", '
    '"priority": "low" | "normal" | "high" | "urgent"}.'
)
REPLY_SYSTEM = (
    "You are a senior support agent writing the reply the customer will read. Never promise "
    "refunds, credits, or legal or compliance commitments. Reply with the message text only."
)
RESEARCH_SYSTEM = (
    "You research support tickets. List the facts a support agent needs to answer this ticket, "
    "as at most five short bullet points."
)
QA_SYSTEM = (
    "You are a QA reviewer for support research notes. Reply in one sentence: say which facts "
    "are missing or need checking again."
)


def _build_handbook() -> str:
    """A deterministic support handbook of roughly 7,000 tokens, built from fleet/data.py.

    Static on purpose: an identical large prefix sent on every call without prompt caching is
    exactly what the cache-miss investigator looks for.
    """
    parts = ["SUPPORT POLICY HANDBOOK", "", "Section A. Knowledge base articles", ""]
    for article in data.KB_ARTICLES:
        parts += [f"A.{article.id} {article.title}", article.body, ""]
    parts += ["Section B. Past cases", ""]
    for ticket in data.TICKETS:
        parts += [f"B.{ticket.id} {ticket.subject}", ticket.body, ""]
    parts += ["Section C. Customer plans", ""]
    for customer_id, record in data.CRM.items():
        parts.append(f"C.{customer_id}: plan {record['plan']}, {record['seats']} seats, "
                     f"CSM {record['csm']}")
    parts += ["", "Section D. Policy clauses", ""]
    topics = ("billing disputes", "single sign-on", "API rate limits", "webhook delivery",
              "data exports", "password resets", "plan changes", "user provisioning",
              "compliance requests", "account deletion", "security incidents", "outages",
              "invoices", "seat true-ups", "data residency", "audit logs")
    rules = (
        "Confirm the requester is an owner or admin of the workspace before acting on {t}.",
        "Quote the matching knowledge base article by its id when replying about {t}.",
        "Never promise a refund, credit or contractual change while handling {t}.",
        "Escalate {t} to the account team when the customer is on an enterprise plan.",
        "Record the ticket id, the affected workspace and the first reported time for {t}.",
        "Reply to urgent {t} within one business hour and to normal ones within one day.",
        "Ask for logs, screenshots or request ids only when they are needed to resolve {t}.",
        "Do not share another customer's configuration as an example when explaining {t}.",
        "Close {t} only after the customer confirms the fix or after seven quiet days.",
        "Link the public status page in every reply about {t} during an active incident.",
        "Route legal or regulatory questions raised during {t} to the compliance team.",
        "Summarize the resolution of {t} in one sentence on the internal case note.",
    )
    clause = 1
    for topic in topics:
        for rule in rules:
            parts.append(f"D.{clause:03d} {rule.format(t=topic)}")
            clause += 1
    return "\n".join(parts)


HANDBOOK = _build_handbook()


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


# Baseline (no investigator): healthy classifier -> responder traffic for three customers.
# In Revenium: the normal-looking traces the wasteful patterns are compared against.
# Bedrock: same metadata; only the client and model ids change.
def baseline_trace(trace: Trace) -> None:
    ticket = trace.tickets[0]
    root = trace.call("classifier", "ticket-classification", "Classify ticket",
                      TRIAGE_SYSTEM, _ticket_text(ticket))
    trace.call("responder", "response-drafting", "Draft reply", REPLY_SYSTEM,
               f"Write the reply to this ticket in at most 80 words.\n\n{_ticket_text(ticket)}",
               parent=root.response_id)


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


# Error concentration (error-concentration-v1): one agent keeps calling a model that fails.
# In Revenium: ERROR records with zero tokens, all on the researcher, retried once.
# Bedrock: an unknown model id fails the same way (check that ERROR records arrive, see docs).
def error_concentration(trace: Trace) -> None:
    prompt = _ticket_text(trace.tickets[0])
    for attempt in range(2):  # the retry fails exactly like the first attempt
        trace.call("researcher", "kb-research", "Research ticket", RESEARCH_SYSTEM, prompt,
                   retry_number=attempt, model=ERROR_MODEL, expect_error=True)


# Max tokens cutoff (max-tokens-cutoff-v1): long answers requested under a tiny token limit.
# In Revenium: stop reason max_tokens on a responder and a qa-reviewer call. The yellow
# max_tokens warnings in the run output are expected here. Bedrock: same max_tokens value.
def max_tokens_cutoff(trace: Trace) -> None:
    ticket = trace.tickets[0]
    trace.call("responder", "response-drafting", "Draft reply", REPLY_SYSTEM,
               f"Write a thorough reply of about 350 words with a heading per issue.\n\n"
               f"{_ticket_text(ticket)}", max_tokens=CUTOFF_MAX_TOKENS)
    trace.call("qa-reviewer", "qa-review", "QA review", QA_SYSTEM,
               f"Write a detailed, itemized review of every issue in this ticket.\n\n"
               f"{_ticket_text(ticket)}", max_tokens=CUTOFF_MAX_TOKENS)


# Model task fit (model-task-fit, model-awareness-v1): the expensive model does trivial work.
# In Revenium: ticket-classification calls on the responder's model instead of the cheap one.
# Bedrock: point the classifier at your largest Claude model id.
def model_task_fit(trace: Trace) -> None:
    expensive = config.AGENT_MODELS["responder"][1]
    for ticket in trace.tickets[:2]:
        trace.call("classifier", "ticket-classification", "Classify ticket", TRIAGE_SYSTEM,
                   _ticket_text(ticket), model=expensive)


# Outdated model (outdated-model-versions-v1, provider-deprecated-model-v1, model-awareness-v1):
# In Revenium: an older model on each provider, chained classifier -> researcher.
# Bedrock: use an older Claude model id that your account can still call.
def outdated_model(trace: Trace) -> None:
    ticket = trace.tickets[0]
    root = trace.call("classifier", "ticket-classification", "Classify ticket", TRIAGE_SYSTEM,
                      _ticket_text(ticket), model=LEGACY_ANTHROPIC_MODEL)
    trace.call("researcher", "kb-research", "Research ticket", RESEARCH_SYSTEM,
               _ticket_text(ticket), parent=root.response_id, model=LEGACY_OPENAI_MODEL)


# Bloated context (prompt-cache-miss-v1, context-ratio-anomaly): a huge static prompt, no cache.
# In Revenium: about 7,000 input tokens for a one-sentence answer, three times per trace.
# Bedrock: send the handbook as a plain system string and never add a cache point.
def bloated_context(trace: Trace) -> None:
    for ticket in trace.tickets[:3]:
        # HANDBOOK goes in as a plain string, so no cache_control block is ever attached.
        trace.call("responder", "response-drafting", "Answer from handbook", HANDBOOK,
                   f"In one sentence, name the handbook section that applies to this ticket.\n\n"
                   f"{_ticket_text(ticket)}")


# Fan-out (agent-graph-amplification): redundant and circular agent calls in one trace.
# In Revenium: three identical researcher calls under one classifier, then a QA <-> researcher
# loop four calls deep. Bedrock: same parent_transaction_id chaining.
def fan_out(trace: Trace) -> None:
    ticket = trace.tickets[0]
    root = trace.call("classifier", "ticket-classification", "Classify ticket", TRIAGE_SYSTEM,
                      _ticket_text(ticket))
    for _ in range(3):  # the same research request sent three times in parallel branches
        research = trace.call("researcher", "kb-research", "Research ticket", RESEARCH_SYSTEM,
                              _ticket_text(ticket), parent=root.response_id)
    # QA keeps sending the facts back to research, so the two agents bounce the ticket.
    parent = research.response_id
    for agent, task, name, system in (
        ("qa-reviewer", "qa-review", "QA review", QA_SYSTEM),
        ("researcher", "kb-research", "Research again", RESEARCH_SYSTEM),
        ("qa-reviewer", "qa-review", "QA review", QA_SYSTEM),
        ("researcher", "kb-research", "Research again", RESEARCH_SYSTEM),
    ):
        parent = trace.call(agent, task, name, system, _ticket_text(ticket), parent=parent).response_id


PATTERNS = (
    Pattern("baseline", "Baseline", (), 2, baseline_trace, customers=BASELINE_CUSTOMERS),
    Pattern("retry-waste", "Retry waste", ("retry-waste",), 4, retry_waste),
    Pattern("error-concentration", "Error concentration", ("error-concentration-v1",), 2,
            error_concentration, expected_errors=2),
    Pattern("max-tokens-cutoff", "Max tokens cutoff", ("max-tokens-cutoff-v1",), 2,
            max_tokens_cutoff),
    Pattern("model-task-fit", "Model task fit", ("model-task-fit", "model-awareness-v1"), 2,
            model_task_fit),
    Pattern("outdated-model", "Outdated model",
            ("outdated-model-versions-v1", "provider-deprecated-model-v1", "model-awareness-v1"), 2,
            outdated_model),
    Pattern("bloated-context", "Bloated context", ("prompt-cache-miss-v1", "context-ratio-anomaly"), 3,
            bloated_context),
    Pattern("fan-out", "Fan-out", ("agent-graph-amplification",), 8, fan_out),
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
        ("Error model", f"openai/{ERROR_MODEL} (fails on purpose)"),
        ("Legacy models", f"anthropic/{LEGACY_ANTHROPIC_MODEL}, openai/{LEGACY_OPENAI_MODEL}"),
        ("Model-task fit", f"anthropic/{config.AGENT_MODELS['responder'][1]} on ticket-classification"),
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
