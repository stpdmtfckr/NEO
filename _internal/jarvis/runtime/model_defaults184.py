"""One-time lightweight model migration with explicit custom choices preserved."""
from __future__ import annotations

from jarvis.ollama_models import DEFAULT_CHAT_MODEL

REVISION = 1
STOCK_CHAT_MODELS = frozenset({
    "gemma4", "gemma4:latest", "qwen3-vl", "qwen3-vl:4b", "qwen3.5", "qwen3.5:4b",
})
MANAGED_ALIASES = frozenset({"", "neo-agent", "neo-agent-ft", "neo-agent-ft-prev", "neo-agent-ft-candidate"})


def _section(data, key):
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _name(value):
    return str(value or "").strip().casefold().removesuffix(":latest")


def migrate(data: dict, persisted: dict) -> None:
    # Inspect the unmerged profile: packaged defaults already carry the revision.
    # A later deliberate selection, including an old stock name, must survive.
    revision = persisted.get("model_defaults_revision")
    if type(revision) is int and revision >= REVISION:
        return
    for section, key in (("ollama", "chat_model"), ("local_chat", "model")):
        selected = _section(persisted, section).get(key)
        if isinstance(selected, str) and selected.strip().casefold() in STOCK_CHAT_MODELS:
            data.setdefault(section, {})[key] = DEFAULT_CHAT_MODEL
    previous = _section(persisted, "llm_agent")
    agent = data.setdefault("llm_agent", {})
    managed = (_name(previous.get("model")) in MANAGED_ALIASES
               and _name(previous.get("fine_tuned")) in MANAGED_ALIASES)
    old_base = str(previous.get("base_model", "") or "").strip().casefold()
    old_size = str(previous.get("size", "") or "").strip().casefold()
    if managed and old_base in {"", "qwen2.5:1.5b", DEFAULT_CHAT_MODEL} and old_size in {"", "1.5b", "0.5b"}:
        agent.update(size="0.5b", base_model=DEFAULT_CHAT_MODEL)
        # Old managed tuned aliases have no evidence that they used the new base.
        # Leave the installed weights intact; a new verified training run can select them.
        agent["fine_tuned"] = ""
    agent["allow_large_fallback"] = False
    for section in ("llm_agent", "local_chat"):
        current = data.setdefault(section, {})
        if str(current.get("keep_alive", "")).strip().casefold() in {"10m", "15m", "30m", "60m"}:
            current["keep_alive"] = "5m"
    data["model_defaults_revision"] = REVISION
