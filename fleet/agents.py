"""The four support-triage agents and the per-ticket pipeline.

classifier (Anthropic) -> researcher (OpenAI) -> responder (Anthropic) -> qa-reviewer (OpenAI),
which makes two calls: the QA review, then a QA note that carries the review's score.
Each call's parent is the response id of the call before it in the same ticket, so every
ticket forms one 5-call tree in Revenium. This module never prints: every finished call is
reported through `on_call(ticket, result)` and every tool lookup through
`on_tool(ticket, tool_id, detail)`.
"""
import json
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from fleet import config, data, llm

CATEGORIES = ("billing", "technical", "account", "compliance", "how-to")
PRIORITIES = ("low", "normal", "high", "urgent")


def parse_json(text: str) -> dict:
    """The JSON object in a model reply, tolerating prose or code fences around it; {} if none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def clamp_score(value) -> Optional[float]:
    """A 0.0-1.0 score, or None for missing, boolean, non-numeric or non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, number))


@dataclass
class TicketResult:
    ticket: data.Ticket
    customer: data.Customer
    job_id: str
    outcome: str  # "DEFLECTED", "ESCALATED" or "FAILED"
    category: str = "general"
    priority: str = "normal"
    needs_human: bool = False
    verdict: Optional[str] = None
    score: Optional[float] = None
    calls: list = field(default_factory=list)  # llm.LLMResult, in call order
    tools: list = field(default_factory=list)  # tool ids, in call order
    failed_agent: Optional[str] = None
    error: Optional[str] = None


@dataclass
class _State:
    run_id: str
    ticket: data.Ticket
    customer: data.Customer
    job_id: str
    parent: Optional[str] = None  # response id of the previous call in this ticket
    calls: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    current_agent: str = ""


def _call(state, agent, task_type, transaction_name, system, prompt, on_call,
          quality: Optional[float] = None) -> llm.LLMResult:
    state.current_agent = agent
    ticket, customer = state.ticket, state.customer
    metadata = llm.build_usage_metadata(
        agent=agent,
        task_type=task_type,
        trace_id=state.job_id,
        trace_name=f"{customer.org} – {ticket.id}: {ticket.subject}",
        transaction_name=transaction_name,
        organization_name=customer.org,
        subscription_id=customer.subscription_id,
        subscriber_id=ticket.requester_id,
        subscriber_email=ticket.requester_email,
        agentic_job_id=state.job_id,
        agentic_job_name=f"{ticket.id}: {ticket.subject}",
        agentic_job_type=config.JOB_TYPE,
        parent_transaction_id=state.parent,  # None for the classifier: each ticket is its own tree
        retry_number=0,
        response_quality_score=quality,  # only the QA note sends one; None sends nothing
    )
    result = llm.complete(agent, system, prompt, metadata)
    state.parent = result.response_id
    state.calls.append(result)
    on_call(state.ticket, result)
    return result


def _header(state) -> str:
    t, c = state.ticket, state.customer
    return f"Ticket: {t.id}\nCustomer: {c.org} ({c.tier})\nSubject: {t.subject}\n"


def classify_agent(state, on_call) -> dict:
    system = (
        "You triage customer support tickets. Reply with one JSON object only, no prose: "
        '{"category": "billing" | "technical" | "account" | "compliance" | "how-to", '
        '"priority": "low" | "normal" | "high" | "urgent", "needs_human": true | false}. '
        "Set needs_human to true for legal, compliance, security-incident or data-deletion requests."
    )
    prompt = f"{_header(state)}\n{state.ticket.body}"
    parsed = parse_json(_call(state, "classifier", "ticket-classification", "Classify ticket",
                              system, prompt, on_call).text)
    category = str(parsed.get("category", "")).strip().lower()
    priority = str(parsed.get("priority", "")).strip().lower()
    needs_human = parsed.get("needs_human")
    return {
        "category": category if category in CATEGORIES else "general",
        "priority": priority if priority in PRIORITIES else "normal",
        "needs_human": needs_human is True
        or (isinstance(needs_human, str) and needs_human.strip().lower() == "true"),
    }


def research_agent(state, cls, on_call, on_tool) -> str:
    ticket = state.ticket
    crm = llm.crm_lookup(ticket.customer_id)  # metered tool
    state.tools.append("crm-lookup")
    on_tool(ticket, "crm-lookup", f"{crm['plan']} plan, {crm['seats']} seats")
    articles = llm.kb_search(f"{ticket.subject} {cls['category']}")  # metered tool
    state.tools.append("kb-search")
    on_tool(ticket, "kb-search", f"{len(articles)} article{'' if len(articles) == 1 else 's'}")
    if articles:
        kb = "\n".join(f"- {a.id} {a.title}: {a.body}" for a in articles)
    else:
        kb = "No KB articles matched."
    system = (
        "You are a support researcher. Using only the CRM record and KB articles provided, write "
        "3-6 short fact bullets (under 120 words total) the reply writer needs. "
        "Note any gaps the material does not cover. No greeting, no reply draft."
    )
    prompt = (
        f"{_header(state)}"
        f"Category: {cls['category']}, priority: {cls['priority']}, "
        f"needs human: {'yes' if cls['needs_human'] else 'no'}\n\n"
        f"CRM record: plan {crm['plan']}, seats {crm['seats']}, CSM {crm['csm']}, "
        f"open incidents {crm['open_incidents']}\n\n"
        f"KB articles:\n{kb}\n\n"
        f"Ticket:\n{ticket.body}"
    )
    return _call(state, "researcher", "kb-research", "Research ticket", system, prompt, on_call).text


def respond_agent(state, cls, facts, on_call) -> str:
    system = (
        "You are a senior support agent writing the reply the customer will read. Use only the "
        "facts provided. Never promise refunds, credits, or legal or compliance commitments; say "
        "the right team will follow up instead. "
        f"Style: {state.customer.reply_style} Reply with the message text only."
    )
    prompt = (
        f"{_header(state)}"
        f"Category: {cls['category']}, priority: {cls['priority']}\n\n"
        f"Ticket:\n{state.ticket.body}\n\n"
        f"Facts:\n{facts}"
    )
    return _call(state, "responder", "response-drafting", "Draft reply", system, prompt, on_call).text


def review_agent(state, facts, draft, on_call) -> dict:
    system = (
        "You are a QA reviewer for support replies. Reply with one JSON object only, no prose: "
        '{"verdict": "PASS" | "ESCALATE", "score": 0.0-1.0, "reason": "<under 25 words>"}. '
        "ESCALATE when the draft is not grounded in the facts, promises refunds, credits or legal "
        "or compliance commitments, or the ticket needs a human."
    )
    prompt = f"{_header(state)}\nFacts:\n{facts}\n\nDraft reply:\n{draft}"
    parsed = parse_json(_call(state, "qa-reviewer", "qa-review", "QA review",
                              system, prompt, on_call).text)
    verdict = str(parsed.get("verdict", "")).strip().upper()
    reason = parsed.get("reason")
    return {
        "verdict": "PASS" if verdict == "PASS" else "ESCALATE",
        "score": clamp_score(parsed.get("score")),
        "reason": str(reason) if reason is not None else "",
    }


def qa_note_agent(state, grade, on_call) -> str:
    """Second qa-reviewer call: a short internal note that carries the review's score.

    The score cannot ride on the review call that produces it, because Revenium reads a call's
    metadata when the call starts. The note is always made, so every ticket has 5 calls; with
    no score (missing or unparseable), no quality score is sent.
    """
    score = grade["score"]
    system = (
        "You write internal notes for the support team. Reply with one or two sentences only. "
        "If the QA verdict is ESCALATE, say what a human agent must handle. "
        "If it is PASS, say why the reply can be sent."
    )
    prompt = (
        f"{_header(state)}"
        f"QA verdict: {grade['verdict']}\n"
        f"QA score: {f'{score:.2f}' if score is not None else 'n/a'}\n"
        f"QA reason: {grade['reason']}"
    )
    return _call(state, "qa-reviewer", "qa-note", "QA note", system, prompt, on_call,
                 quality=score).text


def run_ticket(run_id: str, ticket: data.Ticket, on_call: Callable, on_tool: Callable) -> TicketResult:
    """Run one ticket through all four agents. A failed call ends the ticket as FAILED."""
    customer = data.customer(ticket)
    job_id = f"{run_id}-{ticket.id}"  # trace id and job id; valid job-id characters by construction
    state = _State(run_id, ticket, customer, job_id)
    # The try/except sits INSIDE the job scope: no exception may escape it, so a failed ticket
    # is reported by the app, never automatically by the job scope.
    with llm.ticket_job(job_id, name=f"{ticket.id}: {ticket.subject}"):
        try:
            cls = classify_agent(state, on_call)
            facts = research_agent(state, cls, on_call, on_tool)
            draft = respond_agent(state, cls, facts, on_call)
            grade = review_agent(state, facts, draft, on_call)
            qa_note_agent(state, grade, on_call)
            passed = grade["verdict"] == "PASS" and not cls["needs_human"]
            return TicketResult(
                ticket, customer, job_id,
                outcome="DEFLECTED" if passed else "ESCALATED",
                category=cls["category"],
                priority=cls["priority"],
                needs_human=cls["needs_human"],
                verdict=grade["verdict"],
                score=grade["score"],
                calls=state.calls,
                tools=state.tools,
            )
        except Exception as exc:  # provider errors, timeouts, Revenium blocks; KeyboardInterrupt still propagates
            return TicketResult(
                ticket, customer, job_id,
                outcome="FAILED",
                calls=state.calls,
                tools=state.tools,
                failed_agent=state.current_agent,
                error=f"{type(exc).__name__}: {exc}",
            )
