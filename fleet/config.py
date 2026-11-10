"""Agent-to-model mapping and the fixed metadata values every call carries."""
import os

# Reads env at import time: import this module only after load_dotenv() has run.


def _model(env_name: str, default: str) -> str:
    # An empty MODEL_* line in .env falls back to the default.
    return os.getenv(env_name, "").strip() or default


AGENT_MODELS = {  # agent -> (provider, model); override any model in .env
    "classifier": ("anthropic", _model("MODEL_CLASSIFIER", "claude-haiku-5-5")),
    "researcher": ("openai", _model("MODEL_RESEARCHER", "gpt-6-luna")),
    "responder": ("anthropic", _model("MODEL_RESPONDER", "claude-sonnet-5-5")),
    "qa-reviewer": ("openai", _model("MODEL_QA", "gpt-6-luna")),
}

# Older / non-reasoning models that do not take an effort parameter.
NO_EFFORT_PREFIXES = ("claude-haiku-4", "claude-3", "gpt-4o", "gpt-4.1")
MAX_TOKENS = 2048

PRODUCT_NAME = "AI Support Triage"
TRACE_TYPE = "support-triage"
AGENT_VERSION = "1.0.0"
JOB_TYPE = "support_ticket"
ENVIRONMENT = os.getenv("REVENIUM_ENVIRONMENT", "").strip() or "demo"
