"""Fictional customers, support tickets, KB articles and CRM records for the demo.

Every organization is "Demo – " prefixed and every requester email ends in
.example: each run creates these organizations and subscribers in the Revenium
tenant it reports to, so nothing here may name a real company or person.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Customer:
    id: str
    org: str
    subscription_id: str
    tier: str
    reply_style: str


@dataclass(frozen=True)
class Ticket:
    id: str
    customer_id: str
    requester_id: str
    requester_email: str
    subject: str
    body: str


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    tags: tuple
    body: str


_GROWTH_STYLE = "Clear reply with numbered steps, up to 180 words."

CUSTOMERS = {
    # Globex is the deliberately expensive customer: long multi-issue tickets
    # plus a thorough reply style drive the most responder tokens.
    "globex": Customer(
        "globex",
        "Demo – Globex Corporation",
        "globex-enterprise",
        "enterprise",
        "Thorough, structured reply: a short heading per issue, numbered steps, up to 350 words.",
    ),
    "initech": Customer("initech", "Demo – Initech", "initech-growth", "growth", _GROWTH_STYLE),
    "acme": Customer(
        "acme",
        "Demo – Acme Rockets",
        "acme-starter",
        "starter",
        "Short, friendly reply of at most 80 words.",
    ),
    "umbrella": Customer("umbrella", "Demo – Umbrella Health", "umbrella-growth", "growth", _GROWTH_STYLE),
}


def _ticket(id, customer_id, requester_id, subject, body):
    return Ticket(id, customer_id, requester_id, f"{requester_id}@{customer_id}.example", subject, body)


# Interleaved by customer so `--tickets 2` spans two customers and the expensive one comes first.
TICKETS = (
    _ticket(
        "TKT-1001",
        "globex",
        "marta.kessler",
        "SSO outage, duplicate invoice and API rate limits",
        "Hi team, we have three problems this morning and our leadership is asking for answers. "
        "First, single sign-on has been failing for about two hours. Our Okta users get a SAML "
        "error after the redirect and land back on the login page, so roughly 900 people cannot "
        "reach the dashboard. Nothing changed on our side as far as we know, but our identity team "
        "rotated a signing certificate last week. Second, finance noticed that the October invoice "
        "was charged twice on our corporate card, two identical amounts a minute apart. We need the "
        "duplicate charge reversed and a corrected invoice for our auditors before month end. Third, "
        "our integration team keeps hitting HTTP 429 responses from the reporting API during the "
        "nightly sync. It started after we added two new regions, and the sync now takes six hours "
        "instead of forty minutes. Can you tell us what the current rate limits are for our "
        "enterprise plan, whether they can be raised, and how we should structure retries so we do "
        "not make things worse? Please treat the SSO issue as the top priority, since it blocks "
        "everyone. Thanks, Marta, IT operations lead.",
    ),
    _ticket(
        "TKT-1002",
        "acme",
        "leo.brandt",
        "How do I reset my password?",
        "Hi! I forgot my password and the reset email never shows up in my inbox. I checked spam "
        "too. How do I reset my password or get a new reset link? Thanks.",
    ),
    _ticket(
        "TKT-1003",
        "initech",
        "nadia.ferro",
        "API rate limit errors and missing webhooks",
        "Hello, since Monday our order service has been getting HTTP 429 rate limit errors from "
        "your API a few times an hour, usually when our batch job starts at the top of the hour. "
        "At the same time we noticed that some webhooks for completed orders never reach our "
        "endpoint. Our logs show no incoming request at all for those events, and our endpoint "
        "answers in under 200 milliseconds. Are the two problems related? What are the limits on "
        "the growth plan, and is there a way to replay the webhooks we missed so our records match? "
        "Thanks, Nadia.",
    ),
    _ticket(
        "TKT-1004",
        "umbrella",
        "samir.holt",
        "HIPAA data export request",
        "Hello, our compliance office needs a complete export of all records that contain patient "
        "information for an internal HIPAA audit. Please tell us how to request a full data export, "
        "which formats are available, how the export is encrypted in transit and at rest, and how "
        "long the download link stays valid. We also need written confirmation of where the data "
        "is stored. The audit deadline is the end of next week, so please let us know the expected "
        "turnaround and who on your side handles compliance requests. Regards, Samir, privacy officer.",
    ),
    _ticket(
        "TKT-1005",
        "globex",
        "devin.osei",
        "Audit log export, seat true-up and webhook retries",
        "Hi support, I have three items from our quarterly review. One: our security team needs to "
        "export the audit logs for the last ninety days, including admin actions and API key "
        "changes. The export button in the admin console only gives us thirty days, and the CSV "
        "seems to drop the actor email column. Is there a way to get the full range, ideally in a "
        "machine-readable format our SIEM can ingest? Two: our seat count grew from 1,050 to about "
        "1,200 during the quarter after the acquisition of a smaller team. We want to understand how "
        "the seat true-up works, when it is billed, and whether the new seats are prorated or "
        "charged for the full term. Three: our webhook endpoint was down for maintenance for about "
        "forty minutes on Saturday. We want to know how many times you retry failed webhook "
        "deliveries, over what period, and whether events that exhausted their retries can be "
        "replayed. We would also like to know if we can configure a secondary endpoint for "
        "failover, and whether retries are signed the same way as the original delivery. "
        "Thanks in advance, Devin, platform engineering.",
    ),
    _ticket(
        "TKT-1006",
        "acme",
        "ivy.tanaka",
        "Exporting contacts to CSV",
        "Quick question: how do I export my contact list to a CSV file? I need it for a mailing "
        "this week and I cannot find the export option in the menu.",
    ),
    _ticket(
        "TKT-1007",
        "initech",
        "paul.radev",
        "Plan upgrade proration question",
        "Hi, we are on the growth plan and want to upgrade to enterprise in the middle of our "
        "billing cycle because we need SSO. How does proration work when we upgrade mid-cycle? "
        "Will we be charged the full enterprise price for this month, or only the difference for "
        "the remaining days? Also, do our current seats carry over automatically, and does the "
        "upgrade reset our renewal date? Finance needs an estimate before they approve the change, "
        "so a short breakdown would help a lot. Thanks, Paul.",
    ),
    _ticket(
        "TKT-1008",
        "globex",
        "marta.kessler",
        "SCIM provisioning, data residency and renewal",
        "Hello again, three topics ahead of our renewal, and I am writing on behalf of IT, legal and "
        "procurement so we can handle them together. First, SCIM provisioning from our directory "
        "stopped creating new users yesterday. Deprovisioning still works, but new hires get a 409 "
        "conflict error in the provisioning log, and we onboard about forty people every Monday. "
        "Second, our legal team asked whether we can keep all of our data in the EU region, "
        "including backups and logs, and what it takes to migrate the existing workspace. They want "
        "to know if a migration causes downtime and whether integrations need to be reconfigured "
        "afterwards. Third, our contract renews in two months. We would like to understand the "
        "options for a multi-year term, whether the enterprise rate limits and support response "
        "times change with a longer term, and who our account team contact is for the renewal "
        "conversation. Procurement also asked for the current price sheet and any volume discount "
        "tiers above 1,200 seats, since we expect to add another business unit next year. The SCIM problem is the urgent one because it blocks onboarding next week. "
        "Best regards, Marta, IT operations lead.",
    ),
    _ticket(
        "TKT-1009",
        "umbrella",
        "grace.mbeki",
        "Account deletion and BAA request",
        "Hi, we are consolidating vendors and want to close one of our two workspaces. Please "
        "delete the account for our research workspace and all of its data, and send written "
        "confirmation once the deletion is complete, including backups. Separately, for the "
        "workspace we keep, our legal team needs a signed business associate agreement (BAA) "
        "before we store any more patient data. Who do we contact to start the BAA process, and "
        "how long does it usually take? Thank you, Grace, compliance manager.",
    ),
)

KB_ARTICLES = (
    Article(
        "KB-01",
        "Single sign-on (SSO) troubleshooting",
        ("sso", "saml", "okta", "login", "outage", "certificate"),
        "Most SSO failures after a redirect come from a mismatched SAML signing certificate. "
        "If the identity provider rotated its certificate, upload the new one under Admin > "
        "Security > SSO. Enterprise admins can enable password fallback for owners while the "
        "fix is applied. Check the SSO event log for the exact SAML error.",
    ),
    Article(
        "KB-02",
        "Invoices, duplicate charges, refunds",
        ("billing", "invoice", "duplicate", "charge", "refund"),
        "Duplicate card charges are usually an authorization hold that drops within five "
        "business days. If both charges settle, billing support reverses the duplicate and "
        "issues a corrected invoice. Only the billing team can approve refunds or credits; "
        "support collects the invoice number and charge dates.",
    ),
    Article(
        "KB-03",
        "API rate limits",
        ("api", "rate", "limit", "limits", "429", "throttling", "retries"),
        "Rate limits are per workspace: growth allows 600 requests per minute, enterprise 3,000. "
        "A 429 response includes a Retry-After header. Clients should back off exponentially "
        "with jitter and spread batch jobs instead of starting at the top of the hour. "
        "Enterprise limits can be raised through the account team.",
    ),
    Article(
        "KB-04",
        "Webhook delivery, retries, replay",
        ("webhook", "webhooks", "delivery", "retries", "replay", "missing"),
        "Failed webhook deliveries are retried 8 times over 24 hours with exponential backoff. "
        "Events that exhaust their retries appear under Developers > Webhooks > Failed, where "
        "they can be replayed for 30 days. Enterprise workspaces can add a secondary failover "
        "endpoint.",
    ),
    Article(
        "KB-05",
        "Exporting CSV files, audit logs",
        ("export", "exporting", "csv", "audit", "log", "logs", "contacts"),
        "Most lists export to CSV from the list menu > Export. The admin console exports 30 "
        "days of audit logs; the Audit Log API returns up to 365 days as JSON lines for SIEM "
        "ingestion, including the actor email field. Large exports arrive by email as a link "
        "valid for 7 days.",
    ),
    Article(
        "KB-06",
        "Password reset, account access",
        ("password", "reset", "login", "access", "locked"),
        "Users reset their password from the login page with Forgot password. Reset emails "
        "come from no-reply and expire after one hour; if one does not arrive, check spam and "
        "any email allowlist. Workspace admins can send a new reset link from Admin > Users.",
    ),
    Article(
        "KB-07",
        "Plan upgrades, seats, proration, renewal",
        ("plan", "upgrade", "seats", "seat", "proration", "true", "renewal", "contract"),
        "Upgrades take effect immediately and are prorated: you pay the difference for the "
        "remaining days of the cycle. Seats carry over and the renewal date does not change. "
        "Enterprise seat true-ups are billed quarterly at the prorated contract rate. "
        "Multi-year terms are handled by the account team.",
    ),
    Article(
        "KB-08",
        "SCIM user provisioning",
        ("scim", "provisioning", "users", "directory", "onboarding"),
        "SCIM creates, updates and deactivates users from your directory. A 409 conflict on "
        "create usually means a deactivated user with the same email still exists; reactivate "
        "or remove that user and retry the sync. Provisioning logs live under Admin > Security "
        "> SCIM.",
    ),
    Article(
        "KB-09",
        "Compliance: HIPAA, BAA, data residency",
        ("compliance", "hipaa", "baa", "residency", "eu", "gdpr", "encryption"),
        "HIPAA workloads require a signed BAA before patient data is stored; legal handles BAA "
        "requests. Data is encrypted in transit (TLS 1.2+) and at rest (AES-256). EU data "
        "residency, including backups, is available on enterprise plans; migrations are "
        "scheduled with the account team.",
    ),
    Article(
        "KB-10",
        "Account deletion, data retention",
        ("deletion", "delete", "account", "retention", "workspace", "data"),
        "Workspace deletion is permanent and must be requested by an owner. Data is removed "
        "within 30 days and from backups within 90 days, after which a deletion certificate "
        "can be issued. Exports should be taken before deletion is requested.",
    ),
)

CRM = {
    "globex": {"plan": "Enterprise (annual)", "seats": 1200, "csm": "Renata Vogel", "open_incidents": 2},
    "initech": {"plan": "Growth (monthly)", "seats": 85, "csm": "Tomasz Ibarra", "open_incidents": 0},
    "acme": {"plan": "Starter (monthly)", "seats": 6, "csm": "none (self-serve)", "open_incidents": 0},
    "umbrella": {"plan": "Growth (annual)", "seats": 140, "csm": "Keiko Dunmore", "open_incidents": 1},
}


def customer(ticket: Ticket) -> Customer:
    return CUSTOMERS[ticket.customer_id]


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def search_kb(query: str, limit: int = 3) -> list:
    """Articles sharing the most distinct words with the query (title plus tags); [] if none."""
    query_words = _words(query)
    scored = []
    for article in KB_ARTICLES:
        score = len(query_words & _words(" ".join((article.title,) + tuple(article.tags))))
        if score > 0:
            scored.append((score, article))
    scored.sort(key=lambda pair: pair[0], reverse=True)  # stable: ties keep KB_ARTICLES order
    return [article for _, article in scored[:limit]]


def lookup_customer(customer_id: str) -> dict:
    return dict(CRM[customer_id])
