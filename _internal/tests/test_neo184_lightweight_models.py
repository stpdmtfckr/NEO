"""Lightweight model defaults use real isolated profiles and inert requests."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import os

import pytest

from jarvis.config import ConfigManager
from jarvis.ollama_client import OllamaClient, OllamaError
from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
from jarvis.runtime import model_life173 as life
from jarvis.testing.isolation import external_io_blocked

ROOT = Path(__file__).resolve().parents[1]
SMALL = "qwen2.5:0.5b"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(ConfigManager, "apply_autostart", lambda *a: None)
    monkeypatch.setattr(ConfigManager, "apply_wake_service", lambda *a: None)
    cancellation_manager().reset()
    life._PRELOAD_INFLIGHT[0] = False
    life._LAST_PRELOAD[0] = 0
    with external_io_blocked():
        yield
    life._PRELOAD_INFLIGHT[0] = False
    life._LAST_PRELOAD[0] = 0


def config(payload=None):
    if payload is not None:
        folder = Path(os.environ["LOCALAPPDATA"]) / "NeoJarvis"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    return ConfigManager(ROOT / "config/default_settings.json")


def legacy(**extra):
    value = {"settings_version": 179, "ollama": {"chat_model": "qwen3.5:4b", "vision_model": "qwen3.5:4b"},
             "local_chat": {"model": "", "keep_alive": "10m"},
             "llm_agent": {"model": "neo-agent", "size": "1.5b", "base_model": "qwen2.5:1.5b",
                           "fine_tuned": "neo-agent-ft", "allow_large_fallback": True, "keep_alive": "15m"}}
    value.update(extra)
    return value


def assert_small(c):
    assert c.get("ollama.chat_model") == SMALL
    assert c.get("llm_agent.base_model") == SMALL and c.get("llm_agent.size") == "0.5b"
    assert c.get("ollama.vision_model") == "qwen3.5:4b"
    assert c.get("model_defaults_revision") == 1
    assert c.get("llm_agent.allow_large_fallback") is False


def test_fresh_profile_small_text_and_separate_vision_defaults():
    c = config()
    assert_small(c)
    assert c.get("llm_agent.finetune.cpu_threads") == 2
    assert c.get("llm_agent.keep_alive") == c.get("local_chat.keep_alive") == "5m"
    assert c.get("performance.preload_on_wake") is True
    assert c.get("ollama.num_thread") == 4


@pytest.mark.parametrize("stock", ["gemma4", "qwen3-vl:4b", "qwen3.5:4b"])
@pytest.mark.parametrize("version", [0, 14, 179])
def test_real_legacy_profile_reload_save_restart_migrates_stock(stock, version):
    payload = legacy(settings_version=version)
    payload["ollama"]["chat_model"] = stock
    payload["local_chat"]["model"] = stock
    payload["custom_extension"] = {"keep": [1, "original"]}
    c = config(payload)
    assert_small(c)
    assert c.get("local_chat.model") == SMALL
    assert c.get("llm_agent.fine_tuned") == ""
    c.reload()
    assert_small(c)
    c.save(c.data)
    restarted = config()
    assert_small(restarted)
    assert restarted.get("local_chat.model") == SMALL
    assert restarted.get("custom_extension") == payload["custom_extension"]
    assert restarted.get("llm_agent.keep_alive") == restarted.get("local_chat.keep_alive") == "5m"


@pytest.mark.parametrize("key", ["chat", "local", "planner", "tuned", "base"])
def test_old_explicit_custom_choices_survive_migration_and_restart(key):
    payload = legacy()
    if key == "chat":
        payload["ollama"]["chat_model"] = "my/chat:7b"
        path = "ollama.chat_model"
    elif key == "local":
        payload["local_chat"]["model"] = "my/chat:7b"
        path = "local_chat.model"
    else:
        field = {"planner": "model", "tuned": "fine_tuned", "base": "base_model"}[key]
        payload["llm_agent"][field] = "my/chat:7b"
        path = "llm_agent." + field
    c = config(payload)
    assert c.get(path) == "my/chat:7b"
    c.save(c.data)
    assert config().get(path) == "my/chat:7b"


def test_new_deliberate_old_stock_selection_is_not_migrated_again():
    c = config(legacy())
    c.data["ollama"]["chat_model"] = "qwen3.5:4b"
    c.data["local_chat"]["model"] = "qwen3.5:4b"
    c.data["llm_agent"].update(size="1.5b", base_model="qwen2.5:1.5b")
    c.save(c.data)
    again = config()
    assert again.get("ollama.chat_model") == again.get("local_chat.model") == "qwen3.5:4b"
    assert again.get("llm_agent.base_model") == "qwen2.5:1.5b"


class Response:
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def json(self): return self.payload
    def close(self): pass


class Session:
    def __init__(self, models): self.models = models; self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response({"models": [] if url.endswith("/api/ps") else self.models})
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/api/show"):
            # Deliberately stale larger alias; no default path should use it.
            return Response({"modelfile": "FROM qwen2.5:1.5b"})
        return Response({"message": {"content": '{"reply":"inert"}'}})
    def close(self): pass


def client(c, names=(SMALL, "neo-agent:latest", "qwen3.5:4b")):
    result = OllamaClient(c)
    result._session = Session([{"name": name} for name in names])
    return result


def test_chat_planner_and_wake_preload_share_exact_small_tag_and_context():
    c = config(legacy())
    ollama = client(c)
    assert ollama.resolve_chat_model() == ollama.resolve_agent_model() == SMALL
    assert life.preload(ollama, background=False, now=100)
    ollama.chat_text([{"role": "user", "content": "inert"}])
    ollama.agent_json([{"role": "user", "content": "inert"}], {})
    sent = [(url, args["json"]) for url, args in ollama._session.calls if "json" in args]
    assert len(sent) == 3 and all(data["model"] == SMALL for _, data in sent)
    assert all(data["keep_alive"] == "5m" and data["options"]["num_ctx"] == 3072 for _, data in sent)
    assert all(data["options"]["num_thread"] == 4 for _, data in sent)
    assert not life.preload(ollama, background=False, now=101)


@pytest.mark.parametrize("mode", ["chat", "planner", "generic_text"])
@pytest.mark.parametrize("legacy_fallback_flag", [True, False])
def test_missing_small_model_never_selects_available_larger_model(mode, legacy_fallback_flag):
    c = config()
    c.data["llm_agent"]["allow_large_fallback"] = legacy_fallback_flag
    ollama = client(c, ("qwen2.5:1.5b", "qwen3.5:4b"))
    resolve = {"chat": ollama.resolve_chat_model, "planner": ollama.resolve_agent_model,
               "generic_text": ollama.resolve_model}[mode]
    with pytest.raises(OllamaError): resolve()
    assert not any(url.endswith("/api/chat") or url.endswith("/api/generate") for url, _ in ollama._session.calls)


def test_custom_planner_is_authoritative_with_small_default_available():
    c = config()
    c.data["llm_agent"]["model"] = "my/custom:7b"
    ollama = client(c, (SMALL, "my/custom:7b"))
    assert ollama.resolve_agent_model() == "my/custom:7b"


def test_explicit_different_size_keeps_its_own_chat_context():
    c = config()
    c.data["local_chat"].update(model="qwen2.5:1.5b", num_ctx=4096, keep_alive="2m")
    ollama = client(c, (SMALL, "qwen2.5:1.5b"))
    ollama.chat_text([{"role": "user", "content": "inert"}])
    payload = next(args["json"] for url, args in ollama._session.calls if url.endswith("/api/chat"))
    assert payload["model"] == "qwen2.5:1.5b"
    assert payload["options"]["num_ctx"] == 4096 and payload["keep_alive"] == "2m"


def test_vision_uses_multimodal_selection_not_small_text_default():
    ollama = client(config())
    assert ollama.resolve_model(require_vision=True) == "qwen3.5:4b"


def test_stale_stock_alias_without_small_base_cannot_be_warmed():
    c = config(legacy())
    ollama = client(c, ("neo-agent:latest", "qwen3.5:4b"))
    with pytest.raises(OllamaError):
        ollama.resolve_agent_model()
    assert life.preload(ollama, background=False, now=100)
    assert not any(url.endswith("/api/generate") or url.endswith("/api/chat") for url, _ in ollama._session.calls)


def proven_client():
    c = config()
    c.data["llm_agent"]["fine_tuned"] = "neo-agent-ft"
    proof = {"hf_base": "Qwen/Qwen2.5-0.5B-Instruct", "ollama_base": SMALL,
             "model_digest": "sha256:" + "a" * 64, "base_url": c.get("ollama.base_url"), "run_key": "verified-run"}
    c.data["llm_agent"]["fine_tuned_provenance"] = {"neo-agent-ft": proof}
    ollama = client(c)
    ollama._session.models.append({"name": "neo-agent-ft:latest", "digest": "a" * 64})
    return c, ollama, proof


def test_real_trained_alias_requires_promoted_exact_base_and_current_digest():
    _, ollama, _ = proven_client()
    assert ollama.resolve_agent_model() == "neo-agent-ft:latest"
    assert not any(url.endswith("/api/show") for url, _ in ollama._session.calls)


@pytest.mark.parametrize("field,value", [
    ("hf_base", "Qwen/Qwen2.5-1.5B-Instruct"), ("ollama_base", "qwen2.5:1.5b"),
    ("model_digest", "sha256:" + "b" * 64), ("base_url", "http://localhost:22434"), ("run_key", ""),
])
def test_stale_trained_provenance_cannot_select_different_weights(field, value):
    _, ollama, proof = proven_client()
    proof[field] = value
    assert ollama.resolve_agent_model() == SMALL


def test_changed_alias_tag_digest_is_not_hidden_by_inventory_cache():
    _, ollama, _ = proven_client()
    assert ollama.resolve_agent_model() == "neo-agent-ft:latest"
    ollama._session.models[-1]["digest"] = "b" * 64
    assert ollama.resolve_agent_model() == SMALL


def test_prompt_wrapper_named_fine_tuned_is_not_weight_training(monkeypatch):
    c = config()
    c.data["llm_agent"]["fine_tuned"] = "neo-agent-ft"
    ollama = client(c, (SMALL, "neo-agent-ft:latest"))
    monkeypatch.setattr(ollama._session, "post", lambda *a, **kw: Response({"modelfile": "FROM " + SMALL}))
    assert ollama.resolve_agent_model() == SMALL


def test_provenance_change_invalidates_queued_planner_request():
    _, ollama, proof = proven_client()
    request = ollama._capture_request("agent")
    proof["run_key"] = "new-run"
    with pytest.raises(OperationCancelled): ollama._check_request(request)
