# Surfacing Revenium AI Insights from a test tenant

Revenium AI Insights runs a set of investigators over your metered traffic and reports waste: retries, errors, outdated models, cut-off replies, runaway spend and similar problems. Each investigator only reports a finding once a pattern reaches a minimum volume and dollar amount. A quiet non-production tenant never gets there, and real LLM calls cannot get there affordably.

This repo offers two tools:

- **`seed_insights.py` (recommended).** Posts synthetic, backdated metering records straight to Revenium, sized to reach each investigator's minimums. No LLM calls.
- **`run_fleet.py --insights-load`.** Makes real, paid LLM calls that show each pattern on real metered traffic, at volumes too small to trigger findings.

## Seed synthetic Insights data (recommended)

`seed_insights.py` builds synthetic completion records and posts them to the Revenium metering API with the metering key the demo already uses (`REVENIUM_METERING_API_KEY`). It makes no LLM calls and needs no provider keys. Timestamps are backdated up to 90 days, so the time-series investigators get their history in one run.

> **Warning: `--fire` writes synthetic spend into your tenant.** A full run sends **6,416 records with about $1,800 of synthetic spend at list prices**. That spend appears in the tenant's cost dashboards, customer reports and model reports, exactly like real usage. This tool cannot delete it. **Use a sandbox tenant, never production.**

### Three steps

1. **Dry run (free, sends nothing).** Builds every record and prints, per scenario, the record count, the date span, the estimated list-price spend and the investigator it targets.

   ```bash
   python seed_insights.py
   ```

2. **Send the records.** Prints the synthetic-spend warning with your masked key and the metering URL, then asks you to type `yes` (or pass `--yes`). At the default 10 records per second, a full run takes about 11 minutes.

   ```bash
   python seed_insights.py --fire
   ```

3. **Verify.** Starts an Insights run and reports which scenarios produced findings.

   ```bash
   python seed_insights.py --verify --tag <tag printed by --fire>
   ```

   Or do both in one go: `python seed_insights.py --fire --verify`. It waits 90 seconds for ingestion before it starts the Insights run.

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--fire` | off | Send the records. Without it, the script is a dry run |
| `--yes` | off | With `--fire`: skip the `yes` prompt. Required in non-interactive shells |
| `--verify` | off | Start an Insights run and report FOUND / not found per scenario |
| `--detectors` | all | Comma list of scenario keys, for example `retry-waste,fan-out` |
| `--tps` | 10 | Records sent per second (maximum 50) |
| `--period-days` | 30 | `--verify`: how many days back the Insights run looks (1–90) |
| `--environment` | `insights-seed` | Environment label on every record |
| `--tag` | `seed-YYYYMMDD-xxxx` | Run tag on agents, error reasons and names. Pass the same tag to `--verify` to see whether findings mention it |

Exit codes: `0` means success (a dry run, a full delivery, or a completed Insights run even when some scenarios are not found). `1` means a missing or rejected key, an invalid URL, any record that failed to send, or an Insights run that failed or timed out. `130` means you pressed Ctrl-C.

Each record is sent exactly once, with no retries. If some fail, the script prints up to five failures and exits `1`. Fix the cause before you send again, because a second `--fire` adds another full set of synthetic spend.

### How to recognise seeded data

- Environment `insights-seed` and trace type `insights-seed`
- Product `Insights Seed`
- Organizations and trace names that start with `Insights Seed –`, for example `Insights Seed – dominant customer (seed-20261120-ab12)`
- Agent names, error reasons and names that end in the run tag `seed-YYYYMMDD-xxxx`

## Investigator map

Revenium does not publish exact detection thresholds. The volumes below are what the seeder sends, sized to clear each investigator's known minimums. They do not guarantee a finding: a given Insights run may still report nothing for some scenarios.

| Investigator id | Seeder scenario (`--detectors` key) | What is seeded (minimum volume) | Real-call pattern (`--insights-load`) |
|---|---|---|---|
| `agent-graph-amplification` | `fan-out` | A 12-deep parent chain, 12 fan-out traces (3 with 15 children, 9 with 3) and a 6-step loop between two agents; 102 records. **Computed hourly** from the hour that just completed, so the rows are stamped inside the current UTC hour and sent first | `fan-out` |
| `error-concentration-v1` | `error-concentration` | 600 errors, 75% on one agent, unique error reason | `error-concentration` |
| `outdated-model-versions-v1` | `outdated-model` | 1,000 calls (≥200 required) on `gpt-4o`, about 120M input tokens | `outdated-model` |
| `prompt-cache-miss-v1` | `prompt-cache-miss` | 600 calls at 125K input tokens with zero cache reads or writes, plus 150 smaller calls the week before | `bloated-context` |
| `retry-waste` | `retry-waste` | ≥10 retries above a 5% retry rate, retry rows ≥ $50 (40 records) | `retry-waste` |
| `model-task-fit` | `model-task-fit` | Same agent and task on two models, 160 calls each: 2.5% failures on one, 30% failures or retries on the other | `model-task-fit` |
| `model-awareness-v1` | `model-awareness` | 140 calls (≥120 required) on `claude-3-5-sonnet-latest` | `model-task-fit`, `outdated-model` |
| `subscriber-concentration` | `customer-concentration` | 12 organizations, one with about 98% of the spend; 330 records | Globex carries most of every round |
| Credential usage spike (category `credential_spike`) | `credential-spike` | One credential: 7 calls a day for 30 days, then 32 a day for 30 days (more than 3×); 1,170 records | none |
| `max-tokens-cutoff-v1` | `max-tokens-cutoff` | 80 `TOKEN_LIMIT` rows of 120. **This one may not fire:** the metering API takes the `TOKEN_LIMIT` stop reason, and this investigator may only recognise provider-native stop reasons | `max-tokens-cutoff` |
| `dormant-spend-v1` | `dormant-spend` | An agent with about $38 of spend and no outcomes (120 records), plus a customer active 30–90 days ago and silent since (140 records) | none |
| `spend-trajectory-v1` | `spend-trajectory` | 8 jobs a day for 28 days, then 22 a day for 30 days at a flat cost per job; 884 records | none |
| `unit-economics-drift-v1` | `unit-economics-drift` | 10 jobs a day for 28 days, then 14 a day for 30 days at about 17× the cost per job; 700 records | none |
| `context-ratio-anomaly` | not seeded | n/a | `bloated-context` |
| `provider-deprecated-model-v1` | not seeded | n/a | `outdated-model` with `INSIGHTS_LEGACY_*` set to a deprecated model |

You can list the investigators your tenant runs with the Revenium API/MCP tool `get_ai_insights`, action `list_investigators`.

## Check the results

`--verify` calls `POST /api/v2/insights/runs` on the Revenium app. For the default API address (`https://api.revenium.ai`) that is `https://app.revenium.ai`. If your tenant uses another address, set `REVENIUM_INSIGHTS_APP_URL` in `.env`. The script then:

- follows a run that is already active (HTTP 409) instead of starting a second one; that run's period may differ
- polls the run every 15 seconds, for up to 20 minutes
- prints the finding categories and a FOUND / not found line per selected scenario
- exits `0` when the run completes, even if some scenarios are not found

It authenticates with `REVENIUM_INSIGHTS_API_KEY` when set, otherwise with the metering key. If the Insights API rejects the metering key, set `REVENIUM_INSIGHTS_API_KEY` to a key that can start Insights runs.

Period guidance:

- The default period is 30 days, which covers every scenario.
- Per-call scenarios (error concentration, outdated model, prompt cache miss, retry waste, model-task fit, model awareness) are stamped within the last 7 days. If one is missing, run `python seed_insights.py --verify --period-days 7 --tag <tag>`.
- For `fan-out`, verify again after the next full UTC hour.
- Do not filter the run by organization; that removes the customer-concentration signal.

**In the Revenium app:** open AI Insights, start a new run over the same period, and leave the organization filter empty. Labels in the app may differ from the ones used here.

**Through the Revenium API / MCP tools:** call `manage_ai_insights` with action `trigger_run`, `period_start` and `period_end` (ISO 8601 or `yyyy-MM-dd`, UTC), and optional filters such as `filter_environment` (`insights-seed`) or `filter_trace_type`. The run is asynchronous: poll `get_ai_insights` with action `get_run` and the returned `run_id` until the status is `completed`, `partial`, `failed` or `cancelled`, then read the findings. Check the current parameter schema first, for example with `tool_introspection`, action `get_tool_metadata`.

## AWS Bedrock

The seeded records are provider-agnostic. Insights reads the same metering fields whether a record came from Anthropic, OpenAI or Bedrock through the Revenium SDK, so the findings show what Bedrock traffic at that volume would get.

- Keep the seeded model ids for `outdated-model` (`gpt-4o`) and `model-awareness` (`claude-3-5-sonnet-latest`). Those investigators need Revenium to know a newer successor model.
- To make other scenarios look like your Bedrock traffic, copy the provider and model strings from one of your real Bedrock records in Revenium into that scenario's builder in `seed_insights.py`. Not every Bedrock model id has been tested against every investigator.

## See the patterns on real calls (`--insights-load`)

This is the secondary path. Real-call volumes are below the detection minimums, so use the load mode to see what each pattern looks like on real, metered calls, not to trigger findings. The seeder's backdated time-series scenarios replace the multi-day schedule this mode would otherwise need.

```bash
python run_fleet.py --insights-load --rounds 2
```

- `--rounds` defaults to 2 and is capped at 10. Each round makes **29 LLM calls**; the preflight prints the exact count before any paid call.
- The expensive responder model (`MODEL_RESPONDER`) handles 12 of the 29 calls per round and most of the cost.
- Every call carries trace type `insights-load`, agentic job type `insights_load`, product `AI Support Triage`, the demo organizations (`Demo – Globex Corporation` with most traffic, `Demo – Initech`, `Demo – Umbrella Health`) and the fleet's own agents and task types.
- Some output is deliberate: two yellow `✗ (expected)` lines per round (a model that does not exist) and `Warning: ... hit max_tokens` lines.
- Exit codes match the fleet's: `0` success (deliberate failures do not count), `1` an unexpected failure or failed metering delivery, `130` Ctrl-C. The last line must be `Metering OK: sent=N failed=0 unsent=0`.

| Variable | Default | Purpose |
|---|---|---|
| `INSIGHTS_ERROR_MODEL` | `gpt-insights-load-missing-model` | Invalid on purpose; every call to it fails |
| `INSIGHTS_LEGACY_ANTHROPIC_MODEL` | `claude-haiku-4-5` | Older Anthropic model for the outdated-model pattern |
| `INSIGHTS_LEGACY_OPENAI_MODEL` | `gpt-4o-mini` | Older OpenAI model for the outdated-model pattern |

The patterns live in [`fleet/insights_load.py`](../fleet/insights_load.py). Each is one function plus one entry in its `PATTERNS` table:

- **baseline:** a classifier and a responder call per customer. Healthy traffic to compare against.
- **retry-waste:** a classifier root, then three identical responder drafts with `retry_number` 0, 1 and 2.
- **error-concentration:** two researcher calls on `INSIGHTS_ERROR_MODEL`; both fail and send ERROR records with zero tokens.
- **max-tokens-cutoff:** two calls with `max_tokens` 64 that ask for long answers, so both stop at the limit.
- **model-task-fit:** two classifications on the responder's expensive model.
- **outdated-model:** a classifier call on `INSIGHTS_LEGACY_ANTHROPIC_MODEL` and a researcher call on `INSIGHTS_LEGACY_OPENAI_MODEL`. Set either to a model on the provider's deprecation list to feed `provider-deprecated-model-v1`.
- **bloated-context:** three responder calls that each send a roughly 7,000-token handbook as a plain system string, with no cache requested, for a one-sentence answer.
- **fan-out:** a classifier root, three identical researcher calls under it, and a four-call QA ↔ researcher loop.

### Porting the real-call patterns to Bedrock

Keep the agent names, task types, trace type and every `usage_metadata` key. Change only the client and the model ids. These facts come from the README of the Revenium Python SDK 0.14.0 (sections "AWS Bedrock" and "Calls the SDK does not meter").

- **Two ways to meter Claude on Bedrock.** Use `anthropic.AnthropicBedrock` with the same `usage_metadata=` keyword this repo uses, or call `boto3.client("bedrock-runtime")` directly and set `REVENIUM_BEDROCK_TRANSPORT=1`.
- **Only Claude models are metered on Bedrock.** Map the `researcher` and `qa-reviewer` roles onto Claude models when you port.
- **Check the error pattern.** The SDK README says calls served through the Bedrock fast path send no error record. Make one call to an invalid model id and confirm an ERROR record appears before you rely on it.
- **Model ids** look like `anthropic.claude-...` or `us.anthropic.claude-...`. Set them through `MODEL_*` and `INSIGHTS_LEGACY_ANTHROPIC_MODEL`.
- **Prompt caching** only applies when a request asks for it. The bloated-context pattern works by never asking.

## Side effects and clean-up

- **Seeder organizations and agents:** each `--fire` creates organizations named `Insights Seed – … (<tag>)` and agents ending in the tag. A new tag creates a new set.
- **Seeder spend:** about $1,800 of synthetic list-price spend per full run, visible in cost dashboards and customer and model reports. Nothing in this repo can delete it.
- **Load-mode data:** `--insights-load` uses the demo organizations, adds the invalid model (ERROR records with zero tokens) and the legacy models to model lists, and creates jobs of type `insights_load` that never get an outcome. It is real, paid LLM usage.
- **Recommendation:** run both tools against a sandbox tenant, never production, so this data stays separate from real usage.
