# Instructions for AI coding agents

You are helping a person set up and run the Revenium Agent Fleet Demo. Follow [README.md](README.md) for every setup step. This file adds the rules that apply to you as an agent.

## Hard rules

1. **Never handle API keys.** Never ask for a key in chat. Never read, print, `cat`, `grep` or summarize `.env`, and never write a key value into any file. If a key is missing or wrong, tell the person which variable to fix and let them edit `.env` themselves.
2. **Ask before spending money.** Every run makes real, paid LLM calls. Get the person's go-ahead before running anything except `python run_fleet.py --help`.
3. **Don't change the demo code** (`run_fleet.py`, `fleet/`) to make a run succeed. Every supported fix is a setting in `.env`, which the person edits. The code is what the person is evaluating.
4. **Never retry paid commands in a loop.** If a run fails, stop, diagnose it with the table below, and ask before running again.

## Setup procedure

Run every command from the repository root.

1. **Check Python.** Run `python3 --version`. The demo needs 3.10 or newer. On macOS the built-in `python3` is 3.9, so look for `python3.12` (or `python3.11` / `python3.10`). If none exists, tell the person to install Python 3.12 and stop.
2. **Create the virtual environment and install** (README Step 1):
   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
   Call `.venv/bin/python` directly so you don't depend on an activated shell. On Windows, use `.venv\Scripts\python.exe`.
3. **Create `.env`** only if it does not exist yet: `cp .env.example .env`. Never overwrite an existing `.env`.
4. **Hand off to the person.** Tell them to open `.env` and fill in the four keys in the REQUIRED box at the top: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `REVENIUM_METERING_API_KEY` (`hak_`/`rev_`) and `REVENIUM_WRITE_API_KEY` (`rev_sk_`, a different key from the metering key). Wait until they confirm they have done it.
5. **Run the setup check** once they agree. It makes 4 tiny paid calls:
   ```bash
   .venv/bin/python run_fleet.py --check
   ```
6. **Run the demo** once they agree. Suggest `--tickets 2` for a first try, then the full run:
   ```bash
   .venv/bin/python run_fleet.py --tickets 2
   .venv/bin/python run_fleet.py
   ```
   A full run makes 45 LLM calls and takes 1–2 minutes.

## How to tell whether a run worked

Read the output. Don't guess.

- **Preflight block** (printed before any paid call). Keys are masked, which is expected. Check these rows:
  - `Outcomes` should read `on (...)`. `OFF` means the write key is empty. `WARNING ... does not start with rev_sk_` means the wrong key is in that slot.
  - `Base URL` must not contain `INVALID`.
- **Each call** prints a `✓` line. A red `✗` line names the agent, the model and the provider error.
- **The last line** must be `Metering OK: sent=N failed=0 unsent=0`. `Metering FAILED` means Revenium did not accept the records.
- **Exit code:** `0` means success. `1` means a ticket failed or metering delivery failed. `130` means the person pressed Ctrl-C.
- **The fleet run** should also print `Outcomes reported: N of N tickets`.

## Diagnosing failures

Match the error against the **Troubleshooting** table in [README.md](README.md) and give the person the fix it lists. The most common cases:

| Output | What to tell the person |
|---|---|
| `Missing <VAR>` | Fill in that variable in `.env` |
| `✗ ... 401` / `AuthenticationError` | That provider's key is wrong. Paste a new one into `.env` |
| `✗ ... insufficient_quota` / `429` | That provider account needs billing credit, or wait and retry later |
| `✗ ... 404` / model not found | Set a fallback model in `.env`, e.g. `MODEL_RESPONDER=claude-sonnet-4-6` or `MODEL_RESEARCHER=gpt-5-mini` (fallbacks are listed at the bottom of `.env.example`) |
| `Outcome reporting failed ... 403` | `REVENIUM_WRITE_API_KEY` must be a `rev_sk_` write key from the same tenant, and `REVENIUM_TEAM_ID` should be empty |
| `Metering FAILED` with 401 | The Revenium metering key is wrong or from another tenant |

For more detail, the person can set `REVENIUM_LOG_LEVEL=DEBUG` in `.env` before a `--check` run. The SDK redacts credentials in that output.

## After a successful run

Give the person:
- the **Run ID** (printed at the start and in the `Metering OK` line)
- the **"Where to look in Revenium"** lines from the end of the output

Data appears in the Revenium dashboard about one minute after the run. The person checks the dashboard. You cannot see it.

## If asked how the integration works

All Revenium code is in [`fleet/llm.py`](fleet/llm.py):
- two middleware imports patch the Anthropic and OpenAI clients
- each call passes `usage_metadata={...}`
- `@meter_tool` records tool calls
- `JobContext.attach(...).report_outcome(...)` reports ticket outcomes

The README section "How the Revenium integration works" explains it. Point there rather than paraphrasing at length.
