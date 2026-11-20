# Surfacing Revenium AI Insights from a test tenant

Revenium AI Insights runs a set of investigators over your metered traffic and reports waste: retries, errors, oversized models, cut-off replies, and similar problems. A non-production tenant usually has too little traffic, and traffic that is too clean, for any investigator to find something. This repo ships an opt-in load profile that produces the patterns each investigator looks for, using the same metering code as the demo fleet. Generate that traffic over several days, then trigger an Insights run over the same window.

## Caveat: detection thresholds are not published

Revenium has not published to us the exact detection thresholds or minimum volumes each investigator needs. The investigator map below says what each investigator looks for, based on its name and purpose. It does not guarantee that a pattern triggers a finding. A given Insights run may report nothing.

The time-series investigators (spend trajectory, unit economics drift, dormant spend) compare periods, so they need traffic spread across several days. This repo has no backfill: Revenium timestamps each record when the call happens, so you cannot create last week's traffic today.

## Run the load profile

```bash
python run_fleet.py --insights-load --rounds 2
```

- `--rounds` defaults to 2. The maximum is 10, and the command refuses larger values before it loads `.env` or makes any call.
- Each round makes **29 LLM calls per round**. Two rounds make 58 calls; ten rounds make 290.
- Before any paid call, the preflight prints the exact count (`Estimated calls`), the hot customer's share, the deliberately failing model, the legacy models, and the model used for the model-task-fit pattern.
- The expensive responder model (`MODEL_RESPONDER`, default `claude-sonnet-5-5`) handles 12 of the 29 calls in each round: 3 baseline replies, 3 retried drafts, 1 cut-off reply, 2 classifications in the model-task-fit pattern, and 3 calls that each send a roughly 7,000-token handbook. Those 12 calls account for most of each round's cost.

Every call carries:

| Field | Value |
|---|---|
| Trace type | `insights-load` |
| Agentic job type | `insights_load` |
| Environment | `REVENIUM_ENVIRONMENT` from `.env` (default `demo`) |
| Product | `AI Support Triage` |
| Organizations | `Demo – Globex Corporation` (most traffic), `Demo – Initech`, `Demo – Umbrella Health` |
| Agents and task types | The fleet's own: `classifier`, `researcher`, `responder`, `qa-reviewer`; `ticket-classification`, `kb-research`, `response-drafting`, `qa-review` |

Some of the output is deliberate:

- Each round prints two yellow `✗ (expected)` lines. The error-concentration pattern calls a model that does not exist.
- The max-tokens-cutoff pattern prints `Warning: ... hit max_tokens` lines.

Exit codes are the same as the fleet's: `0` means success (the deliberate failures do not count), `1` means an unexpected provider failure or failed metering delivery, and `130` means Ctrl-C. The last line must be `Metering OK: sent=N failed=0 unsent=0`.

Three settings in `.env.example` control the deliberate models:

| Variable | Default | Purpose |
|---|---|---|
| `INSIGHTS_ERROR_MODEL` | `gpt-insights-load-missing-model` | Invalid on purpose; every call to it fails |
| `INSIGHTS_LEGACY_ANTHROPIC_MODEL` | `claude-haiku-4-5` | Older Anthropic model for the outdated-model pattern |
| `INSIGHTS_LEGACY_OPENAI_MODEL` | `gpt-4o-mini` | Older OpenAI model for the outdated-model pattern |

## Investigator map

| Investigator id | Display name | What it looks for (our reading) | How this repo produces it | Bedrock porting note |
|---|---|---|---|---|
| `agent-graph-amplification` | Excess agent calls (fan-out/circular) | Traces with more agent calls than the work needs: duplicate branches and agents calling each other in loops | `fan-out` pattern, `fan_out()` | Keep the `parent_transaction_id` chaining; it is plain metadata |
| `context-ratio-anomaly` | Context Ratio Anomaly | Calls whose input is far larger than their output | `bloated-context` pattern, `bloated_context()` | Same: a large system prompt and a one-sentence answer |
| `error-concentration-v1` | Error Concentration | Failed calls clustered on one agent or model | `error-concentration` pattern, `error_concentration()` | An unknown Bedrock model id fails the same way, but confirm ERROR records arrive (see [Porting to Bedrock](#porting-to-bedrock)) |
| `outdated-model-versions-v1` | Outdated Model Versions | Traffic on older model versions when newer ones exist | `outdated-model` pattern, `outdated_model()` | Use an older Claude model id your account can still call |
| `model-awareness-v1` | Model Awareness | Model choice that does not match the work. Fed indirectly by the `model-task-fit` and `outdated-model` patterns | `model_task_fit()` and `outdated_model()` | Follows the two patterns it is fed by |
| `model-task-fit` | Model Task Fit | An expensive model doing simple work, such as classification | `model-task-fit` pattern, `model_task_fit()` | Point the classifier at your largest Claude model id |
| `prompt-cache-miss-v1` | Prompt Cache Miss | A large, identical prompt prefix sent repeatedly without prompt caching | `bloated-context` pattern, `bloated_context()` | Send the handbook as a plain system string with no cache point |
| `retry-waste` | Retry Waste | The same step repeated within one trace (`retry_number` above 0) | `retry-waste` pattern, `retry_waste()` | Same metadata and loop |
| `spend-trajectory-v1` | Spend Trajectory | Spend that grows from one period to the next | Schedule recipe: ramp the rounds over several days | Run the same schedule against Bedrock |
| `subscriber-concentration` | Subscriber Concentration | One customer or subscriber producing most of the traffic | The `customers` column of the pattern table: 25 of the 29 calls in each round are Globex's, from one subscriber (`marta.kessler@globex.example`). Do **not** filter the Insights run by organization, or the comparison disappears | Keep the same customer mix in the metadata |
| `unit-economics-drift-v1` | Unit Economics Drift | Cost per unit of work (per trace or job) rising over time | Schedule recipe: switch the classifier to the expensive model from day 5 | Same recipe with Bedrock model ids |
| `dormant-spend-v1` | Dormant and Underused Spend | Customers, products or workloads that had spend and then went quiet | Schedule recipe: the normal fleet (trace type `support-triage`, and the only traffic for `Demo – Acme Rockets`) runs on days 1–3 only | Same recipe |
| `max-tokens-cutoff-v1` | Responses Cut Off by Max Tokens Limit | Replies that stop because they hit the max tokens limit | `max-tokens-cutoff` pattern, `max_tokens_cutoff()` | Pass the same small `max_tokens` |
| `provider-deprecated-model-v1` | Provider-deprecated model in production use | Calls to models the provider has deprecated | `outdated-model` pattern, `outdated_model()`, if `INSIGHTS_LEGACY_*` names a deprecated model | See the note below the table |

**Provider-deprecated model.** Set `INSIGHTS_LEGACY_ANTHROPIC_MODEL` or `INSIGHTS_LEGACY_OPENAI_MODEL` to a model on that provider's deprecation list that still serves requests. The investigator's display name says "in production use", so it may only look at production traffic; we do not know. Do not relabel non-production traffic as production just to trigger it.

You can list the investigators your tenant runs with the Revenium API/MCP tool `get_ai_insights`, action `list_investigators`.

## The patterns

All patterns live in [`fleet/insights_load.py`](../fleet/insights_load.py). Each is one function plus one entry in the `PATTERNS` table, which names the investigators it feeds. Every call goes through `llm.build_usage_metadata` and `llm.complete` in [`fleet/llm.py`](../fleet/llm.py), with `trace_type="insights-load"`. Each pattern run is its own trace and agentic job, with id `<run id>-r<round>-<pattern>-<customer>`.

### baseline

`baseline_trace()`. Healthy traffic to compare against; it feeds no investigator directly. Two calls per customer, for Globex, Initech and Umbrella: `classifier` on its configured model, then `responder` (reply of up to 80 words) with the classifier call as its parent.

### retry-waste

`retry_waste()`. Four calls: a `classifier` root, then three identical `responder` "Draft reply" calls. All three have the root as parent and `retry_number` 0, 1 and 2. The story is a strict validator that rejects the first two drafts. Feeds `retry-waste`.

### error-concentration

`error_concentration()`. Two `researcher` calls on `INSIGHTS_ERROR_MODEL` with `retry_number` 0, then 1. Both fail on purpose. Each sends one ERROR record with zero tokens, and the run prints them as `✗ (expected)`. Feeds `error-concentration-v1`.

### max-tokens-cutoff

`max_tokens_cutoff()`. Two calls with `max_tokens` set to 64: a `responder` asked for a thorough 350-word reply (Anthropic), and a `qa-reviewer` asked for a detailed, itemized review (OpenAI). Both stop at the limit. Feeds `max-tokens-cutoff-v1`.

### model-task-fit

`model_task_fit()`. Two `classifier` calls with task type `ticket-classification`, on two different Globex tickets, using the responder's model (`MODEL_RESPONDER`) instead of the classifier's. Feeds `model-task-fit` and `model-awareness-v1`.

### outdated-model

`outdated_model()`. A `classifier` call on `INSIGHTS_LEGACY_ANTHROPIC_MODEL`, then a `researcher` call on `INSIGHTS_LEGACY_OPENAI_MODEL` with the classifier call as its parent. Feeds `outdated-model-versions-v1`, `provider-deprecated-model-v1` and `model-awareness-v1`.

### bloated-context

`bloated_context()`. Three `responder` calls, each on a different Globex ticket, asking for one sentence that names the applicable handbook section. The system prompt is `HANDBOOK`: a fixed support handbook of 28,115 characters (about 7,000 tokens) built from the demo's knowledge base, past tickets, CRM plans and generated policy clauses. It goes in as a plain string, so no prompt cache is ever requested. Feeds `prompt-cache-miss-v1` and `context-ratio-anomaly`.

### fan-out

`fan_out()`. Eight calls: a `classifier` root; three identical `researcher` calls, each with the root as parent; then a four-call loop (`qa-reviewer` → `researcher` → `qa-reviewer` → `researcher`) in which each call's parent is the call before it. The story is a QA agent that keeps sending the facts back. Feeds `agent-graph-amplification`.

## Porting to Bedrock

Keep the agent names, task types, trace type and every `usage_metadata` key. Change only the client and the model ids. The facts below come from the README of the Revenium Python SDK 0.14.0 (`revenium-python-sdk`, sections "AWS Bedrock" and "Calls the SDK does not meter").

- **Two ways to meter Claude on Bedrock.** Use `anthropic.AnthropicBedrock` with the same `usage_metadata=` keyword this repo uses; it is metered once `revenium_middleware.anthropic` is imported. Or call `boto3.client("bedrock-runtime")` directly (`invoke_model`, `invoke_model_with_response_stream`, `converse`, `converse_stream`) and set `REVENIUM_BEDROCK_TRANSPORT=1`.
- **Only Claude models are metered on Bedrock.** Raw boto3 calls to other Bedrock model families send no usage record. This repo runs the `researcher` and `qa-reviewer` roles on OpenAI; map those roles onto Claude models when you port.
- **Check the error pattern.** A call the provider refuses normally sends one record with stop reason `ERROR` and zero tokens. The SDK README also says that "calls served through the Bedrock fast path ... send no error record". Before you rely on the error-concentration pattern, make one call to an invalid model id and confirm that an ERROR record appears in Revenium.
- **Bedrock model ids** look like `anthropic.claude-...` or, for cross-Region inference profiles, `us.anthropic.claude-...`. Set them through `MODEL_*` and `INSIGHTS_LEGACY_ANTHROPIC_MODEL`.
- **Prompt caching.** Claude prompt caching only applies when a request asks for it (a `cache_control` block on the Messages API). The bloated-context pattern works by never asking, so keep it that way when you port.

We make no other claims about Bedrock behavior here.

## Schedule it over several days

The per-call investigators can fire after one run. The time-series investigators need a trend, so spread the traffic over a week:

| Day | Load profile (3 times a day) | Normal fleet (once a day) | `.env` change |
|---|---|---|---|
| 1 | `--rounds 1` | yes | none |
| 2 | `--rounds 1` | yes | none |
| 3 | `--rounds 2` | yes | none |
| 4 | `--rounds 2` | no | none |
| 5 | `--rounds 3` | no | set `MODEL_CLASSIFIER` to the responder model |
| 6 | `--rounds 3` | no | (keep) |
| 7 | `--rounds 3` | no | (keep), then revert after the window |

- **Spend trajectory:** the rounds ramp from 1 to 3, so daily load spend roughly triples (87 calls on day 1, 261 calls on days 5–7).
- **Dormant spend:** the normal fleet runs only on days 1–3. Afterwards, trace type `support-triage` and `Demo – Acme Rockets` go quiet. Acme never appears in the load profile, so its spend stops completely.
- **Unit economics drift:** from day 5, the person running the test edits `.env` to set `MODEL_CLASSIFIER` to the responder model (for example `claude-sonnet-5-5`). Cost per trace rises because every classification now runs on the expensive model. Set it back when the window ends.

Over the 7 days, the load profile makes 1,305 calls, and the fleet adds 45 calls a day on days 1–3.

A crontab for days 1–2 (`crontab -e`). Use absolute paths; `run_fleet.py` finds `.env` next to itself:

```cron
# Revenium Insights load profile: 08:00, 13:00 and 18:00 every day
0 8,13,18 * * * /path/to/agent-fleet-demo/.venv/bin/python /path/to/agent-fleet-demo/run_fleet.py --insights-load --rounds 1 >> /path/to/agent-fleet-demo/insights-load.log 2>&1
# Normal fleet once a day: delete this line after day 3
30 9 * * * /path/to/agent-fleet-demo/.venv/bin/python /path/to/agent-fleet-demo/run_fleet.py >> /path/to/agent-fleet-demo/insights-load.log 2>&1
```

Change `--rounds 1` to `--rounds 2` on day 3 and to `--rounds 3` on day 5. The log contains only masked keys (first and last four characters), the same output you see in the terminal.

On Windows, create the same three daily triggers in Task Scheduler, with `.venv\Scripts\python.exe` as the program and `run_fleet.py --insights-load --rounds 1` as the arguments.

When the window ends, remove the cron lines (or Task Scheduler tasks). Every scheduled run spends money.

## Trigger an Insights run on demand

Insights runs analyze a time period. Set the period to cover the whole load window.

**In the Revenium app:** open AI Insights, start a new run, set the period to cover the load window, and choose filters. Labels in the app may differ from the ones used here.

**Through the Revenium API / MCP tools:**

1. Call `manage_ai_insights` with action `trigger_run`. Required: `period_start` and `period_end`, as ISO 8601 or `yyyy-MM-dd` (UTC). Optional filters:
   - `filter_agent` (list)
   - `filter_product_id` (list of product ids)
   - `filter_trace_type` (list)
   - `filter_consuming_org_id` (list of organization ids)
   - `filter_environment` (string)
   - `exclude_investigator_ids` (list)
2. The run is asynchronous. `trigger_run` returns a run id with status `running`.
3. Poll `get_ai_insights` with action `get_run` and that `run_id` until the status is `completed`, `partial`, `failed` or `cancelled`. Then read the findings.

Before you call it, check the current parameter schema, for example with `tool_introspection`, action `get_tool_metadata`, for `manage_ai_insights`.

We recommend two runs over the same period:

1. **Per-call investigators:** `filter_trace_type` = `["insights-load"]`, plus `filter_environment` if you set one.
2. **Time-series investigators:** `filter_environment` only, with no trace-type filter, so spend trajectory, unit economics drift and dormant spend also see the normal fleet traffic (`support-triage`).

Do not filter either run by organization; that removes the subscriber-concentration signal.

## Side effects and clean-up

- **Organizations:** the load profile uses the demo's existing `Demo – Globex Corporation`, `Demo – Initech` and `Demo – Umbrella Health` organizations. The normal fleet also creates `Demo – Acme Rockets`. All are fictional.
- **Invalid model:** `gpt-insights-load-missing-model` (or your `INSIGHTS_ERROR_MODEL`) appears in model lists with ERROR records and zero tokens.
- **Legacy models:** `claude-haiku-4-5` and `gpt-4o-mini` (or your `INSIGHTS_LEGACY_*` values) appear in model lists and cost reports.
- **Jobs:** each pattern run creates a job of type `insights_load`. These jobs never get a business outcome; only the normal fleet reports outcomes.
- **Stopping:** delete the cron lines or Task Scheduler tasks, revert any `MODEL_CLASSIFIER` change in `.env`, and use a non-production tenant or environment so this traffic stays separate from real usage.
