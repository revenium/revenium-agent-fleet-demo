"""Every Revenium touchpoint in the demo lives in this file.

Importing the two middleware modules patches the Anthropic and OpenAI SDKs so
that each call accepts a `usage_metadata=` keyword and is metered to Revenium.
"""
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import revenium_middleware
import revenium_middleware.anthropic  # noqa: F401  patches anthropic Messages.create
import revenium_middleware.openai  # noqa: F401  patches openai chat.completions.create

import anthropic
import openai

from fleet import config, data

_anthropic = anthropic.Anthropic(timeout=60.0)  # reads ANTHROPIC_API_KEY
_openai = openai.OpenAI(timeout=60.0)  # reads OPENAI_API_KEY


def build_usage_metadata(
    *,
    agent: str,
    task_type: str,
    trace_id: str,
    trace_name: str,
    transaction_name: str,
    organization_name: str,
    subscription_id: str,
    subscriber_id: str,
    subscriber_email: str,
    agentic_job_id: str,
    agentic_job_name: str,
    agentic_job_type: str,
    parent_transaction_id: Optional[str] = None,
    retry_number: int = 0,
    response_quality_score: Optional[float] = None,
) -> dict:
    """The metadata Revenium attaches to one LLM call."""
    metadata = {
        "agent": agent,
        "agent_version": config.AGENT_VERSION,
        "task_type": task_type,
        "trace_id": trace_id,
        "trace_type": config.TRACE_TYPE,
        "trace_name": trace_name,
        "transaction_name": transaction_name,
        "parent_transaction_id": parent_transaction_id,
        "retry_number": retry_number,
        "organizationName": organization_name,
        "productName": config.PRODUCT_NAME,
        "subscription_id": subscription_id,
        "subscriber": {"id": subscriber_id, "email": subscriber_email},
        "environment": config.ENVIRONMENT,
        "agentic_job_id": agentic_job_id,
        "agentic_job_name": agentic_job_name,
        "agentic_job_type": agentic_job_type,
    }
    if response_quality_score is not None:  # 0.0 is a real score and must be kept
        metadata["response_quality_score"] = response_quality_score
    return metadata


# Tools: each call to these two functions reaches Revenium as a tool event. No output_fields
# are passed, so the tool results (CRM record, KB text) never leave the machine.
@revenium_middleware.meter_tool("kb-search", operation="search", agent="researcher", product_name=config.PRODUCT_NAME)
def kb_search(query: str) -> list:
    """Search the demo knowledge base. Revenium records each call as a tool event."""
    return data.search_kb(query)


@revenium_middleware.meter_tool("crm-lookup", operation="lookup", agent="researcher", product_name=config.PRODUCT_NAME)
def crm_lookup(customer_id: str) -> dict:
    """Look up the customer's CRM record. Revenium records each call as a tool event."""
    return data.lookup_customer(customer_id)


def ticket_job(job_id: str, name: str):
    """Scope one ticket to a Revenium agentic job: `with llm.ticket_job(...):`.

    Entering the job needs no key. Tool events made inside it carry the job id, which ties
    them to the ticket. If an exception ever escaped the block, the SDK would report the job
    FAILED on its own; the small retry budget bounds how long that report could block.
    """
    return revenium_middleware.JobContext(
        job_id,
        name=name,
        type=config.JOB_TYPE,
        retry_attempts=2,
        retry_initial_seconds=1.0,
        retry_max_seconds=2.0,
    )


def outcomes_enabled() -> bool:
    """True when REVENIUM_WRITE_API_KEY is set to a non-blank value."""
    # Without a write key, the SDK would fall back to the metering key for outcomes,
    # so the app gates on the write key itself.
    return bool(os.environ.get("REVENIUM_WRITE_API_KEY", "").strip())


def report_outcome(job_id: str, outcome: str, *, reason: Optional[str] = None,
                   metadata: Optional[dict] = None) -> None:
    """Report one ticket's business outcome (DEFLECTED, ESCALATED or FAILED) on its job.

    Revenium creates a job when it ingests the job's first metric, so call this only after
    the metering flush. Raises on failure. The team comes from REVENIUM_TEAM_ID when set;
    otherwise the SDK resolves it from a rev_sk_ write key.
    """
    job = revenium_middleware.JobContext.attach(
        job_id,
        api_key=os.environ.get("REVENIUM_WRITE_API_KEY", "").strip(),  # explicit: never the metering key
        retry_attempts=4,
        retry_initial_seconds=2.0,
        retry_max_seconds=8.0,
    )
    if outcome == "FAILED":
        job.report_outcome("FAILED", outcome_reason=reason[:1000] if reason else None)
    else:
        job.report_outcome("SUCCESS", outcome_type=outcome, metadata=metadata)


@dataclass(frozen=True)
class LLMResult:
    agent: str
    provider: str
    model: str
    text: str
    response_id: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    stop_reason: Optional[str]


def complete(agent: str, system: str, prompt: str, metadata: dict) -> LLMResult:
    """Make one metered LLM call with the model configured for `agent`."""
    if agent not in config.AGENT_MODELS:
        known = ", ".join(config.AGENT_MODELS)
        raise ValueError(f"Unknown agent {agent!r}; known agents: {known}")
    provider, model = config.AGENT_MODELS[agent]
    call = _anthropic_call if provider == "anthropic" else _openai_call
    start = time.perf_counter()
    text, response_id, tokens_in, tokens_out, stop_reason = call(model, system, prompt, metadata)
    latency = time.perf_counter() - start
    return LLMResult(agent, provider, model, text, response_id, tokens_in, tokens_out, latency, stop_reason)


def _anthropic_call(model: str, system: str, prompt: str, metadata: dict):
    effort = {} if model.startswith(config.NO_EFFORT_PREFIXES) else {"output_config": {"effort": "low"}}
    resp = _anthropic.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        usage_metadata=metadata,  # added by revenium_middleware
        **effort,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.id, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason


def _openai_call(model: str, system: str, prompt: str, metadata: dict):
    effort = {} if model.startswith(config.NO_EFFORT_PREFIXES) else {"reasoning_effort": "low"}
    resp = _openai.chat.completions.create(
        model=model,
        max_completion_tokens=config.MAX_TOKENS,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        usage_metadata=metadata,  # added by revenium_middleware
        **effort,
    )
    choice = resp.choices[0]
    # OpenAI says "length" where Anthropic says "max_tokens"; one name keeps the truncation warning uniform.
    stop_reason = "max_tokens" if choice.finish_reason == "length" else choice.finish_reason
    return (
        choice.message.content or "",
        resp.id,
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
        stop_reason,
    )


def metering_ready() -> bool:
    """False when REVENIUM_METERING_API_KEY is missing or lacks the hak_/rev_ prefix."""
    return revenium_middleware.get_client() is not None


def register_error_printer(print_fn: Callable[[str], None]) -> None:
    """Print every failed metering record as it happens. Register once per process."""

    @revenium_middleware.on_metering_error  # runs on the SDK's background sender thread
    def _on_error(event):  # MeteringErrorEvent(error, operation, timestamp)
        # Never raise or exit here: this is not the main thread, and dying here would skip the flush.
        try:
            print_fn(f"Revenium metering error ({event.operation}): {type(event.error).__name__}: {event.error}")
        except Exception:
            pass


@dataclass(frozen=True)
class DeliveryStatus:
    sent: int
    failed: int
    unsent: int
    last_error: Optional[str]
    healthy: bool


def flush_and_status(expected_records: int, timeout_seconds: float = 15.0) -> DeliveryStatus:
    """Send any queued metering records, then report what Revenium accepted.

    `expected_records` is how many records the run produced: one per LLM call (failed calls
    still send an ERROR record) plus one per tool event.
    """
    unsent = revenium_middleware.flush_metering(timeout_seconds=timeout_seconds)
    s = revenium_middleware.get_metering_status()
    # Healthy means every expected record was delivered. unsent == 0 alone is not enough,
    # because rejected records (e.g. 401) are dropped rather than queued. Don't use
    # error_count == 0 either: a transient error that a later retry delivered still counts.
    healthy = unsent == 0 and s.success_count >= max(1, expected_records)
    last_error = f"{type(s.last_error).__name__}: {s.last_error}" if s.last_error else None
    return DeliveryStatus(s.success_count, s.error_count, unsent, last_error, healthy)
