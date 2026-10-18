"""NEO 188: historical records HIST-035 … (plugin policy and consent, launches, translation snapshot, overlay names…)."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_neo169_local_ai import Config, _PLUGIN, _Registry, _make_plugin


class _SavingConfig(Config):
    def save(self, data):
        self.data = json.loads(json.dumps(data))
        self.saved += 1


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch):
    from jarvis.runtime import plugins
    monkeypatch.setattr(plugins, "_DISABLED188", set())


def test_hist035_disable_through_one_registry_is_honoured_by_every_live_registry(tmp_path):
    from jarvis.runtime.plugins import PluginManager
    config = _SavingConfig(tmp_path / "profile")
    root = tmp_path / "plugins"
    _make_plugin(root, "demo", [], _PLUGIN.replace("{name}", "demo"))
    first, second = _Registry(config), _Registry(config)
    manager_a, manager_b = PluginManager(root, config), PluginManager(root, config)
    manager_a.discover(); manager_a.approve("demo")
    manager_a.reload(first); manager_b.reload(second)
    assert first.execute("demo", {"action": "run"}).success and second.execute("demo", {"action": "run"}).success
    manager_a.set_enabled("demo", False)
    manager_a.reload(first)                                     # only the first registry is reloaded
    assert "demo" not in first._tools
    refused = second.execute("demo", {"action": "run"})         # the stale handler in the second one
    assert refused.success is False and refused.data.get("disabled") and "выключен" in refused.text
    manager_a.set_enabled("demo", True)
    assert second.execute("demo", {"action": "run"}).success    # re-enabling restores it everywhere


def _tool_run(config, registry, **args):
    from jarvis.tools import plugins_tool169
    return plugins_tool169.run(config, registry, args)


def test_hist036_consent_to_preview_a_never_stores_updated_b(tmp_path):
    from jarvis.runtime.plugins import PluginManager
    config = _SavingConfig(tmp_path / "profile")
    root = tmp_path / "plugins"
    folder = _make_plugin(root, "demo", [], _PLUGIN.replace("{name}", "demo"))
    registry = _Registry(config)
    registry.plugin_manager = PluginManager(root, config)
    shown = _tool_run(config, registry, action="enable", name="demo")
    assert shown.success                                        # preview A
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(version="2.0", permissions=["clipboard.read"])           # the list now holds B
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = _tool_run(config, registry, action="approve", name="demo")      # «да» to the old preview
    assert result.success is False and result.data.get("preview_changed")
    assert "demo" not in (config.data.get("plugins", {}).get("approved", {}))   # nothing stored for B
    assert "2.0" in result.text
    approved = _tool_run(config, registry, action="approve", name="demo")    # consent to the new preview
    assert approved.success and config.data["plugins"]["approved"]["demo"]["version"] == "2.0"


def test_hist036_manager_rejects_a_mismatching_expected_preview(tmp_path):
    from jarvis.runtime.plugins import PluginManager, PluginPreviewChanged
    config = _SavingConfig(tmp_path / "profile")
    root = tmp_path / "plugins"
    _make_plugin(root, "demo", [], _PLUGIN.replace("{name}", "demo"))
    manager = PluginManager(root, config)
    info = manager.info("demo")
    with pytest.raises(PluginPreviewChanged):
        manager.approve("demo", expected=("other", info.version, tuple(info.permissions), tuple(info.tools)))
    assert config.saved == 0


def test_hist038_cancel_during_the_app_resolver_prevents_the_first_launch(tmp_path, monkeypatch):
    from jarvis.desktop_resolver import ResolvedTarget
    from jarvis.runtime.cancellation import cancellation_manager
    from jarvis.testing.atlas_session import AtlasConfig, AuditAgent
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.tools import implementations
    launches = []
    target = ResolvedTarget("app", str(tmp_path / "orbit.exe"), "Orbit", 0.97)

    def resolver(query, limit=5):
        cancellation_manager().cancel("stop")        # «стоп» while the resolver runs
        return [target]
    with external_io_blocked():
        agent = AuditAgent(AtlasConfig(tmp_path / "profile"))
        try:
            monkeypatch.setattr(implementations, "application_candidates", resolver)
            monkeypatch.setattr(implementations.subprocess, "Popen", lambda *a, **k: launches.append(a))
            cancellation_manager().reset()
            definition = agent.tools._tools["application"]
            result = definition.handler({"action": "open", "name": "Orbit", "kind": "app"})
            assert result.success is False and result.data.get("cancelled") and launches == []
            cancellation_manager().reset()
            monkeypatch.setattr(implementations, "application_candidates", lambda query, limit=5: [target])
            try:
                definition.handler({"action": "open", "name": "Orbit", "kind": "app"})
            except Exception:
                pass                                   # later verification steps are out of scope
            assert len(launches) == 1                  # the normal admission still launches once
        finally:
            agent.database.close()


def test_hist040_one_translation_uses_and_labels_one_endpoint_model_snapshot(monkeypatch):
    from jarvis.runtime import translation184 as tr
    values = {"translation.model": "model-A", "ollama.base_url": "http://A:1", "translation.show_engine": True,
              "app.language": "ru"}
    config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(tr, "model_name", lambda cfg: cfg.get("translation.model"))
    calls = []

    class Http:
        def post(self, url, json=None, timeout=None):
            calls.append((url, json["model"]))
            values.update({"translation.model": "model-B", "ollama.base_url": "http://B:2"})   # Apply during the wait
            return SimpleNamespace(raise_for_status=lambda: None, content=b"1", json=lambda: {"response": "Hello"})
    monkeypatch.setattr(tr, "_chunks", lambda text, size=0: ["Привет", "мир"])
    request = tr.Request("Привет мир", "en", "ru", "ru") if hasattr(tr, "Request") else None
    result = tr.translate_local(config, request, session=Http())
    assert [c[0] for c in calls] == ["http://A:1/api/generate", "http://A:1/api/generate"]
    assert {c[1] for c in calls} == {"model-A"}                                  # no A/B mix inside one request
    assert result.data["model"] == "model-A" and result.data["endpoint"] == "http://A:1"
    assert "model-A" in result.text and "model-B" not in result.text            # the label is what was used


def test_hist043_exact_panel_name_beats_an_alias_and_ties_ask(tmp_path, monkeypatch):
    from jarvis import overlay_state
    monkeypatch.setattr(overlay_state, "publish_overlay_refresh", lambda name="": None)
    store = overlay_state.OverlayStore(tmp_path / "overlays.json")
    store.apply({"action": "show", "name": "Alpha", "title": "Alpha"})          # Alpha is inserted first
    store.apply({"action": "show", "name": "Beta", "title": "Beta"})
    store.apply({"action": "show", "name": "Alpha", "aliases": ["Beta", "Shared"]})   # the colliding aliases
    store.apply({"action": "show", "name": "Beta", "aliases": ["Shared"]})
    store.apply({"action": "hide", "name": "Beta"})
    panels = store.load()["panels"]
    assert panels["Beta"]["visible"] is False and panels["Alpha"]["visible"] is True
    text, data = store.apply({"action": "set_text", "name": "Shared", "text": "x"})
    assert data["requires_clarification"] and set(data["options"]) == {"Alpha", "Beta"}
    assert store.load()["panels"]["Alpha"].get("text", "") != "x"            # nothing changed on a tie
    store.apply({"action": "delete", "name": "Beta"})
    panels = store.load()["panels"]
    assert "Beta" not in panels and "Alpha" in panels
    text, data = store.apply({"action": "hide", "name": "Shared"})             # now unique again (alias of Alpha)
    assert store.load()["panels"]["Alpha"]["visible"] is False
