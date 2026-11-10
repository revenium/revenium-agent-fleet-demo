# Revenium Agent Fleet Demo

A small fleet of AI agents that triages customer support tickets, with **every LLM call metered by Revenium**.

Four agents work each ticket in turn. They use two providers (Anthropic and OpenAI), and the batch covers several fictional customers. When the run finishes, Revenium shows the cost **per agent**, **per ticket (trace)** and **per customer**, along with tool usage, quality scores and business outcomes.

The Revenium integration is short on purpose. It all lives in one file: [`fleet/llm.py`](fleet/llm.py).

```
ticket ──► classifier ──► researcher ──► responder ──► qa-reviewer
           (Anthropic)    (OpenAI)       (Anthropic)    (OpenAI, 2 calls)
                          + kb-search
                          + crm-lookup tools
```

---

> 🤖 **Prefer to let an AI coding agent do the setup?** See [Set up with an AI coding agent](#set-up-with-an-ai-coding-agent).

---

## What you need before you start

| You need | Where to get it |
|---|---|
| **Python 3.10 or newer** (3.12 recommended) | [python.org/downloads](https://www.python.org/downloads/), or `brew install python@3.12` on a Mac |
| **An Anthropic API key** (`sk-ant-...`) | [console.anthropic.com](https://console.anthropic.com/) → API Keys |
| **An OpenAI API key** (`sk-...`) | [platform.openai.com](https://platform.openai.com/) → API keys |
| **A Revenium metering key** (starts with `hak_` or `rev_`) | Revenium app → API Keys |
| **A Revenium write key** (starts with `rev_sk_`) | Revenium app → API Keys (a write-scope key) |

> ⚠️ **The two Revenium keys are different keys.** The metering key records LLM calls. The write key reports ticket outcomes. If you put the metering key in the write-key slot, outcome reporting fails with **403** errors.

> ⚠️ **macOS:** the built-in `python3` is version 3.9, which is **too old**. Install Python 3.12 and use `python3.12` in the commands below.

---

## Setup (about 5 minutes)

Run every command from the project folder, the one that contains this README.

### Step 1: Create a virtual environment and install dependencies

**macOS / Linux:**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Once the environment is active, your prompt starts with `(.venv)`. Each time you open a new terminal, re-run the `source .venv/bin/activate` line (on Windows, the `Activate.ps1` line) before running the demo.

### Step 2: Create your `.env` file

**macOS / Linux:**

```bash
cp .env.example .env
```

**Windows (PowerShell):**

```powershell
copy .env.example .env
```

### Step 3: Fill in the four keys

Open `.env` in any text editor. The four keys you must fill in are in the box at the **top** of the file:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
REVENIUM_METERING_API_KEY=hak_...
REVENIUM_WRITE_API_KEY=rev_sk_...
```

- Paste each key directly after the `=`, with **no quotes and no spaces**.
- **Do not change anything below the box.** Every other setting already has a working default.
- Never commit `.env`. It is already listed in `.gitignore`.

### Step 4: Check your setup (makes 4 tiny paid calls)

```bash
python run_fleet.py --check
```

This makes one very short call with each agent's model and confirms that Revenium received all of them. A healthy run looks like this:

```
Run ID: run-20261110-141502-a1b2
──────────────────── Preflight ────────────────────
Revenium key     hak_…x7Qz
Base URL         https://api.revenium.ai
Environment      demo
Circuit breaker  off
Prompt capture   off
Outcomes         on (write key rev_…9fK2)
classifier       anthropic/claude-haiku-5-5
researcher       openai/gpt-6-luna
responder        anthropic/claude-sonnet-5-5
qa-reviewer      openai/gpt-6-luna
✓ classifier anthropic/claude-haiku-5-5 in=31 out=4 0.84s
✓ researcher openai/gpt-6-luna in=29 out=5 1.02s
✓ responder anthropic/claude-sonnet-5-5 in=31 out=4 1.31s
✓ qa-reviewer openai/gpt-6-luna in=29 out=5 0.97s
Metering OK: sent=4 failed=0 unsent=0 (run run-20261110-141502-a1b2)
```

**What to look for:**

- **Four `✓` lines.** A red `✗` line means that provider rejected the call. See [Troubleshooting](#troubleshooting).
- **`Metering OK` as the last line.** `Metering FAILED` means Revenium did not accept the records.
- **The `Outcomes` row reads `on (...)`.** If it says `OFF` or `WARNING`, fix `REVENIUM_WRITE_API_KEY` in `.env`.

The preflight block always prints **before any paid call**, and it only ever shows the first and last 4 characters of a key.

---

## Set up with an AI coding agent

Instead of following the steps above by hand, you can ask an AI coding agent (Claude Code, Cursor, OpenAI Codex, GitHub Copilot and similar) to do them. The repo includes instructions written for agents:

- **[`AGENTS.md`](AGENTS.md)** holds the setup procedure, how to judge whether a run worked, and how to diagnose failures. It also sets safety rules: the agent never handles your API keys, asks before every paid run, and doesn't change the demo code.
- **[`CLAUDE.md`](CLAUDE.md)** contains one line that points Claude Code at `AGENTS.md`.

### How to use it

1. **Open your agent in the project folder** (the folder that contains this README):
   - **Claude Code:** run `claude` in a terminal in the project folder.
   - **Codex CLI:** run `codex` in a terminal in the project folder.
   - **Cursor / VS Code with Copilot:** open the project folder, then open the agent or chat panel in agent mode.

2. **Paste this prompt:**

   ```
   Read AGENTS.md and follow it to set up and run this demo for me.
   ```

   Starting with "Read AGENTS.md" makes the prompt work whether or not your tool loads the file automatically.

3. **The agent prepares everything except your keys.** It checks your Python version, creates the virtual environment, installs dependencies and creates `.env`.

4. **Paste your four keys into `.env` yourself** when the agent asks (see [Step 3](#step-3-fill-in-the-four-keys)). Then tell the agent you're done.
   > 🔒 Never paste API keys into the chat. A well-behaved agent following `AGENTS.md` won't ask for them. If one does, decline and edit `.env` yourself.

5. **Approve the paid runs.** The agent asks before each one:
   - `--check` first: 4 tiny calls
   - then a short run (`--tickets 2`)
   - then the full run

6. **Read the agent's report.** It gives you the Run ID and the "Where to look in Revenium" hints. Open the Revenium dashboard yourself to see the results (see [See it in Revenium](#see-it-in-revenium)).

If something fails, the agent matches the error against the [Troubleshooting](#troubleshooting) table and tells you what to change in `.env`.

---

## Run the demo

```bash
python run_fleet.py
```

This runs all **9 tickets** for **4 fictional customers** through the four agents: 45 LLM calls and 18 tool calls in total. It takes about 1–2 minutes. The agents use low-effort settings and short replies, so a full run costs a small fraction of a dollar at list prices.

To make a quicker or cheaper run, process only the first N tickets:

```bash
python run_fleet.py --tickets 2
```

### What you will see

One line per LLM call and per tool lookup, then a verdict for each ticket:

```
━━ TKT-1001 · Demo – Globex Corporation · SSO outage, duplicate invoice and API rate limits
✓ classifier anthropic/claude-haiku-5-5 in=412 out=96 1.10s
  tool crm-lookup → Enterprise (annual) plan, 1200 seats
  tool kb-search → 3 articles
✓ researcher openai/gpt-6-luna in=1290 out=310 2.40s
✓ responder anthropic/claude-sonnet-5-5 in=1720 out=540 6.80s
✓ qa-reviewer openai/gpt-6-luna in=2150 out=120 1.90s
✓ qa-reviewer openai/gpt-6-luna in=380 out=40 0.90s
→ TKT-1001 ESCALATED · technical/high · QA PASS 0.86
```

At the end, the run prints:

1. **`Outcomes reported: 9 of 9 tickets`.** The DEFLECTED / ESCALATED / FAILED result for each ticket was sent to Revenium.
2. **A summary table** of calls and tokens per agent and per customer.
3. **"Where to look in Revenium"** hints that include your Run ID.
4. **`Metering OK: ...`** as the last line.

The terminal shows **tokens, never dollars**. Revenium prices every call, so the dashboard is the source of truth for cost.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Everything succeeded |
| `1` | A ticket failed, or metering delivery failed |
| `130` | You pressed Ctrl-C (metering is still flushed first) |

---

## See it in Revenium

Data appears in Revenium about **1 minute** after the run. Copy the **Run ID** from the terminal, then look at:

| Where | What you will see |
|---|---|
| **Traces** | Trace type `support-triage`. Search for your Run ID to see one call tree per ticket: classifier → researcher → responder → qa-reviewer → qa-reviewer |
| **Cost by agent** | Group cost by agent: `classifier`, `researcher`, `responder`, `qa-reviewer`. The responder costs the most. |
| **Cost by customer** | Organizations named `Demo – …`. **Demo – Globex Corporation** costs the most, because its tickets are long and its reply style is thorough. |
| **Tools** | `kb-search` and `crm-lookup` tool events, linked to each ticket's job |
| **Quality** | A response quality score (0–1) on the qa-reviewer's "QA note" call |
| **Jobs / outcomes** | Jobs of type `support_ticket` with a DEFLECTED or ESCALATED outcome |

> **Note:** each run creates (or reuses) four organizations named `Demo – Globex Corporation`, `Demo – Initech`, `Demo – Acme Rockets` and `Demo – Umbrella Health` in the Revenium tenant your key belongs to. All customers, people and emails are fictional (`.example` addresses).

---

## How the Revenium integration works

All of it is in [`fleet/llm.py`](fleet/llm.py). Three pieces matter:

**1. Two imports turn on metering for both providers:**

```python
import revenium_middleware.anthropic   # patches anthropic Messages.create
import revenium_middleware.openai      # patches openai chat.completions.create
```

**2. Each call passes one extra argument, `usage_metadata`:**

```python
client.messages.create(
    model=model,
    messages=[...],
    usage_metadata={
        "agent": "responder",                        # per-agent cost
        "trace_id": "run-...-TKT-1001",              # groups one ticket's calls
        "parent_transaction_id": previous_call_id,   # builds the call tree
        "organizationName": "Demo – Globex Corporation",  # per-customer cost
        "subscriber": {"id": "...", "email": "..."},
        "agentic_job_id": "run-...-TKT-1001",        # ties calls, tools, outcome
        ...
    },
)
```

**3. Tools and outcomes:**

- `@revenium_middleware.meter_tool(...)` on a function records each call to it as a tool event.
- `JobContext.attach(job_id).report_outcome(...)` reports each ticket's business outcome. This step uses the write key.

The rest of the code is plain Python that you can read top to bottom:

| File | What it does |
|---|---|
| [`run_fleet.py`](run_fleet.py) | Command-line entry point: preflight, ticket loop, summary |
| [`fleet/agents.py`](fleet/agents.py) | The four agents (`classify_agent`, `research_agent`, `respond_agent`, `review_agent`, `qa_note_agent`) and the per-ticket pipeline |
| [`fleet/llm.py`](fleet/llm.py) | **All Revenium code**: metering, metadata, tools, outcomes |
| [`fleet/config.py`](fleet/config.py) | Which model each agent uses |
| [`fleet/data.py`](fleet/data.py) | Fictional customers, tickets, knowledge base and CRM records |

No agent framework is used. Prompts and replies are **never sent to Revenium**, because `REVENIUM_CAPTURE_PROMPTS=false`. Only token counts and metadata are sent.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `This demo needs Python 3.10+` | An old Python, often the macOS built-in 3.9 | Install Python 3.12, delete the `.venv` folder, and redo [Step 1](#step-1-create-a-virtual-environment-and-install-dependencies) with `python3.12` |
| `ModuleNotFoundError: No module named 'dotenv'` (or `rich`, `revenium_middleware`) | The virtual environment is not active | Run `source .venv/bin/activate` (Windows: `.venv\Scripts\Activate.ps1`), then try again |
| `Missing ANTHROPIC_API_KEY, ...` | A required key is empty in `.env` | Fill in the four keys at the top of `.env`. Check that the file is named exactly `.env`, not `.env.txt` |
| `REVENIUM_METERING_API_KEY ... was rejected by the Revenium SDK` | The metering key does not start with `hak_` or `rev_` | Copy the metering key again from the Revenium app |
| Red `✗ ... AuthenticationError` / `401` on a provider call | Wrong or expired Anthropic or OpenAI key | Create a new key in that provider's console and paste it into `.env` |
| Red `✗ ... insufficient_quota` / `429` | The OpenAI or Anthropic account has no credit or hit its rate limit | Add billing credit to that account, or wait a minute and retry |
| Red `✗ ... model not found` / `404` | Your account cannot use that model | Set a fallback in `.env`, for example `MODEL_RESPONDER=claude-sonnet-4-6` or `MODEL_RESEARCHER=gpt-5-mini`. The fallbacks are listed at the bottom of `.env.example` |
| `Metering FAILED` with `401` errors | Wrong Revenium metering key, or a key from a different tenant | Copy the metering key again. Check that `REVENIUM_METERING_BASE_URL` is your tenant's address |
| The preflight `Base URL` says `INVALID ... IGNORED` | `REVENIUM_METERING_BASE_URL` lacks `https://` | Use the full address, for example `https://api.revenium.ai` |
| The preflight `Outcomes` row says `OFF` | `REVENIUM_WRITE_API_KEY` is empty | Paste your `rev_sk_` write key into `.env` |
| The preflight `Outcomes` row says `WARNING ... does not start with rev_sk_` | You pasted the metering key (or another key) into the write-key slot | Use the separate **write-scope** key that starts with `rev_sk_` |
| `Outcome reporting failed ... 403` | The write key lacks write permission, or `REVENIUM_TEAM_ID` names a different team | Use a `rev_sk_` write key from the same tenant as the metering key, and leave `REVENIUM_TEAM_ID` empty |
| `Outcome reporting failed ... Could not resolve team_id` | Revenium could not work out your team from the write key | Set `REVENIUM_TEAM_ID` in `.env` to your Revenium team ID |
| Calls show **$0 cost** in Revenium | Revenium's price list does not include that model yet | Switch that agent to an older fallback model (see the `404` row above), or ask your Revenium contact to add pricing for the model |
| **Revenium dashboard is empty** | Data lags about 1 minute; a time filter is hiding the run; or the key belongs to a different tenant | Wait 1–2 minutes, set the time range to "last hour", search for your Run ID, and confirm the last line said `Metering OK` |

Still stuck? Set `REVENIUM_LOG_LEVEL=DEBUG` in `.env` and run `python run_fleet.py --check` again. The SDK then prints each metering request, with credentials redacted.

---

## Command reference

```bash
python run_fleet.py               # full demo: 9 tickets through 4 agents
python run_fleet.py --tickets 2   # only the first 2 tickets
python run_fleet.py --check       # setup check: one short call per model
python run_fleet.py --help        # all options
```
