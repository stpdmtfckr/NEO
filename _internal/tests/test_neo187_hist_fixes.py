"""NEO 187: historical defects HIST-0xx with a code-level fix (component evidence only)."""
from types import SimpleNamespace

import pytest


def _registered(register, **kwargs):
    tools = {}

    class Registry:
        def register(self, definition):
            tools[definition.name] = definition
    register(Registry(), **kwargs)
    return {name: getattr(d, "handler", None) or getattr(d, "function", None) for name, d in tools.items()}


# HIST-051 ------------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [(0, 0), (0.0, 0), (35, 35), (None, 50)])
def test_hist051_explicit_zero_microphone_volume_stays_zero(monkeypatch, value, expected):
    from jarvis.tools.domains import audio
    seen = []

    class FakeInput:
        def set_volume(self, level, device_id=""):
            seen.append(level)
            return {"verified": True, "value": level}
    monkeypatch.setattr(audio, "AudioInputController", FakeInput)
    monkeypatch.setattr(audio.os, "name", "nt")
    config = SimpleNamespace(get=lambda key, default=None: default)
    handler = _registered(audio.register_audio_tools, config=config, send_media_key=lambda *_: None,
                          run_command=lambda *_a, **_k: None)["audio"]
    args = {"action": "set_input_volume"}
    if value is not None:
        args["value"] = value
    result = handler(args)
    assert seen == [expected] and result.success


# HIST-062 ------------------------------------------------------------------------------
def test_hist062_subprocess_timeout_is_classified_by_type():
    import subprocess
    from jarvis.tools.registry import exception_outcome
    assert exception_outcome(subprocess.TimeoutExpired(["ping"], 5)) == "timeout"
    assert exception_outcome(TimeoutError("x")) == "timeout"
    assert exception_outcome(subprocess.CalledProcessError(1, ["ping"])) == "external_service"
    assert exception_outcome(RuntimeError("timed out somewhere")) == "internal_bug"   # text alone decides nothing


# HIST-032 ------------------------------------------------------------------------------
@pytest.mark.parametrize("extra", [["INBOX"], ["inbox", "Входящие"], ["Posteingang"]])
def test_hist032_inbox_alias_is_counted_once(extra):
    from jarvis.integrations.mail_services import selected_mail_folders
    values = {"integrations.mailru.include_inbox": True, "integrations.mailru.additional_folders": extra + ["Работа"]}
    config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    assert selected_mail_folders(config, "mailru") == ["Входящие", "Работа"]
    values["integrations.mailru.include_inbox"] = False
    assert selected_mail_folders(config, "mailru") == ["Входящие", "Работа"]   # the inbox named explicitly, once


# HIST-046 / HIST-047 -------------------------------------------------------------------
@pytest.mark.parametrize("text,shape", [
    ("включи автокликер", "command"), ("автокликер 10 кликов в секунду", "command"), ("стоп автокликер", "command"),
    ("не включай автокликер", "negated"), ("не надо выключать автокликер", "negated"),
    ("напиши в заметку: включи автокликер", "data"), ("скажи «включи автокликер»", "data"),
    ("как работает автокликер", "data"), ("расскажи про автокликер и включи музыку", "data"),
])
def test_hist046_047_autoclicker_shortcut_needs_a_whole_positive_order(text, shape):
    from jarvis.runtime.self_window184 import autoclicker_request_shape
    assert autoclicker_request_shape(text, " ".join(text.casefold().split())) == shape


def test_hist048_stop_is_available_when_the_feature_is_switched_off():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
    block = source[source.index("    def _try_local_voice_control"):source.index("    def _try_local_voice_control") + 4000]
    assert 'and not stopping:' in block and 'if stopping:\n                data = ctl.stop()' in block


# HIST-042 ------------------------------------------------------------------------------
def test_hist042_same_language_is_not_signed_as_a_remote_translation():
    from jarvis.runtime import translation184 as tr

    class Config:
        def get(self, key, default=None):
            return {"translation.show_engine": True}.get(key, default)

    class Boom:
        def translate(self, *a, **k):
            raise AssertionError("no provider call for an unchanged text")
    result = tr.translate(Config(), tr.Request("Hello", "en", "en", "ru"), adapter=Boom())
    assert result.ok and result.engine == "identity" and result.translated == "Hello"
    assert result.data["unchanged"] is True
    assert tr.t("label_searchapi", "ru") not in result.text


# HIST-039 / HIST-041 -------------------------------------------------------------------
class _TrConfig:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_hist039_cancelled_local_translation_never_falls_back_to_remote(monkeypatch):
    from jarvis.runtime import translation184 as tr

    class OperationCancelled(Exception):
        pass

    def cancelled(*_a, **_k):
        raise OperationCancelled()
    monkeypatch.setattr(tr, "_generate", cancelled)
    monkeypatch.setattr(tr, "installed", lambda config: True)
    monkeypatch.setattr(tr, "translate_with_searchapi", lambda *a, **k: pytest.fail("remote fallback after cancel"))
    config = _TrConfig(**{"translation.engine": "auto", "translation.fallback_to_searchapi": True})
    result = tr.translate(config, tr.Request("Привет", "en", "ru", "ru"))
    assert not result.ok and result.data["cancelled"] is True and result.text == tr.t("cancelled187", "ru")


def test_hist041_empty_piece_is_kept_and_marked_partial(monkeypatch):
    from jarvis.runtime import translation184 as tr
    text = "Первый абзац.\n\nВторой абзац."
    monkeypatch.setattr(tr, "_chunks", lambda value: ["Первый абзац.", "Второй абзац."])
    answers = iter(["First paragraph.", ""])
    monkeypatch.setattr(tr, "_generate", lambda *a, **k: next(answers))
    result = tr.translate_local(_TrConfig(), tr.Request(text, "en", "ru", "ru"))
    assert result.ok and result.data["partial"] is True and result.data["untranslated_pieces"] == 1
    assert "First paragraph." in result.translated and "[…] Второй абзац." in result.translated
    assert tr.t("partial187", "ru", n=1, total=2) in result.text


# HIST-044 ------------------------------------------------------------------------------
def test_hist044_empty_checklist_is_reported_as_empty(tmp_path, monkeypatch):
    from jarvis.testing.atlas_session import AtlasConfig
    from jarvis.tools import desktop_features
    monkeypatch.setattr(desktop_features, "request_desktop", lambda *a, **k: True)
    config = AtlasConfig(tmp_path / "profile", "ru")
    tools = {}
    desktop_features.register_desktop_features(SimpleNamespace(config=config, register=lambda t: tools.setdefault(t.name, t)))
    shelf_tool = next(t for name, t in tools.items() if name in {"shelf", "desktop_shelf"})
    empty = shelf_tool.handler({"action": "create", "kind": "checklist", "title": "Покупки"})
    assert empty.success and empty.data["empty"] is True and "пустой список «Покупки»" in empty.text
    filled = shelf_tool.handler({"action": "create", "kind": "checklist", "title": "Дела", "items": [{"text": "a", "checked": False}]})
    assert filled.success and "empty" not in filled.data


# HIST-061 / HIST-063 -------------------------------------------------------------------
def _network_tool(monkeypatch, calls):
    from jarvis.tools.domains import support
    tools = {}

    class Registry:
        def register(self, definition):
            tools[definition.name] = definition

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    support.register_support_tools(Registry(), config=SimpleNamespace(get=lambda k, d=None: d), db=None, ollama=None,
                                   run_command=run, hidden_subprocess_kwargs=dict, known_folder=lambda *a: None,
                                   require_path_value=lambda *a, **k: None, safe_filename=str)
    return tools["network"].handler


@pytest.mark.parametrize("host", ["-t", "--help", "8.8.8.8 -t", "", "bad host", "a" * 300, "-n 100 1.1.1.1"])
def test_hist061_option_like_or_malformed_ping_target_is_refused(monkeypatch, host):
    calls = []
    handler = _network_tool(monkeypatch, calls)
    if host == "":
        handler({"action": "ping", "host": host})       # empty means the default target
        assert calls and calls[-1][-1] == "8.8.8.8"
        return
    with pytest.raises(ValueError):
        handler({"action": "ping", "host": host})
    assert calls == []


@pytest.mark.parametrize("host", ["8.8.8.8", "::1", "example.com", "my-router.local", "ya.ru."])
def test_hist061_ip_and_host_names_are_accepted(monkeypatch, host):
    calls = []
    _network_tool(monkeypatch, calls)({"action": "ping", "host": host})
    assert calls[-1][-1] == host


def test_hist063_resolver_failure_keeps_interface_data(monkeypatch):
    import socket
    from jarvis.tools.domains import support
    monkeypatch.setattr(support.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no dns")))
    result = _network_tool(monkeypatch, [])({"action": "info"})
    assert result.success and result.data["partial"] is True and "addresses" in result.data["missing"]
    assert "interfaces" in result.data and "stats" in result.data


# HIST-065 / HIST-067 -------------------------------------------------------------------
def test_hist067_router_sends_monitor_as_the_schema_string():
    from jarvis.runtime.router import HybridRouter
    routed = HybridRouter().route("сделай скриншот второго монитора")
    assert routed is not None and routed.tool == "screen" and routed.arguments["monitor"] == "2"


def test_hist065_wrong_explicit_monitor_is_an_error_not_all_screens(tmp_path, monkeypatch):
    import inspect
    from jarvis.tools.domains import system_interaction
    source = inspect.getsource(system_interaction)
    block = source[source.index("    def _capture_screen("):source.index("            shot = sct.grab(monitor)")]
    assert 'raise ValueError(f"Монитор «{target}» не найден' in block
    assert "except ValueError:\n                    pass" not in block


# HIST-080 / HIST-081 -------------------------------------------------------------------
def test_hist080_two_reports_in_the_same_second_are_both_kept(tmp_path, monkeypatch):
    from jarvis.runtime import fieldkit173
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path, logs=tmp_path / "logs"), get=lambda k, d=None: d)
    first, _ = fieldkit173.report(config, version="187", log_text="")
    second, _ = fieldkit173.report(config, version="187", log_text="")
    assert first != second and first.exists() and second.exists()


def test_hist081_tail_reads_only_the_end_and_keeps_utf8_clean(tmp_path):
    from jarvis.runtime.fieldkit173 import _read_tail187
    path = tmp_path / "neo.log"
    path.write_bytes(b"x" * 2_000_064 + "ёж".encode("utf-8"))
    tail = _read_tail187(path, 2_000_000)
    assert tail.endswith("ёж") and len(tail.encode("utf-8")) <= 2_000_000
    path.write_bytes("аб".encode("utf-8"))                   # cut in the middle of «а»
    assert _read_tail187(path, 3) == "б"


# HIST-012 ------------------------------------------------------------------------------
def test_hist012_enter_in_a_field_does_not_open_another_settings_page():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from jarvis.ui.responsive178 import NavButton
    button = NavButton("Микрофон и wake word")
    assert not button.autoDefault() and not button.isDefault()


# HIST-016 ------------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_hist016_refused_hint_clear_is_not_reported_as_done(monkeypatch, code):
    from jarvis.tools import ui_control168
    from jarvis.tools.ui_messages168 import msg
    from jarvis.ui import desktop_events
    config = SimpleNamespace(get=lambda key, default=None: code if key == "app.language" else default)
    monkeypatch.setattr(desktop_events, "request_desktop", lambda *a, **k: False)
    refused = ui_control168.hide_hints(config)
    assert not refused.success and refused.text == msg("hints_off_refused", code)
    monkeypatch.setattr(desktop_events, "request_desktop", lambda *a, **k: True)
    accepted = ui_control168.hide_hints(config)
    assert accepted.success and accepted.text == msg("hints_off", code)


# HIST-070 .. HIST-073 ------------------------------------------------------------------
def _assistant(monkeypatch, status, *, resolve=lambda: "neo-agent", started=True, background=("", None, False)):
    from jarvis.neo_llm import jobs, setup
    from jarvis.tools import assistant_model_tool170 as tool
    monkeypatch.setattr(jobs, "read_status", lambda config: status)
    monkeypatch.setattr(setup, "ensure_background", lambda config, **k: started)
    monkeypatch.setattr(setup, "background_status", lambda: background)
    config = SimpleNamespace(get=lambda key, default=None: "ru" if key == "app.language" else default)
    ollama = SimpleNamespace(resolve_agent_model=resolve)
    return lambda action: tool.run(config, {"action": action}, ollama), tool


def test_hist073_lookup_failure_is_not_reported_as_not_installed(monkeypatch):
    def broken():
        raise ConnectionError("no ollama")
    run, tool = _assistant(monkeypatch, {"running": False}, resolve=broken)
    text = run("status").text
    assert tool._t("check_failed", "ru") in text and tool._t("missing", "ru") not in text


def test_hist070_failed_run_is_not_never_run(monkeypatch):
    run, tool = _assistant(monkeypatch, {"running": False, "status": {"stage": "failed", "stage_failed": "train"}})
    text = run("status").text
    assert tool._t("last_failed", "ru", stage="train") in text and tool._t("never", "ru") not in text


def test_hist071_better_but_not_activated_is_its_own_verdict(monkeypatch):
    report = {"candidate": {"do_exact": 0.9, "chat_ok": 1.0}, "current": {"do_exact": 0.8}, "promoted": False, "better": True}
    run, tool = _assistant(monkeypatch, {"running": False, "status": {"stage": "done"}, "last_report": report})
    text = run("status").text
    assert tool._t("activation_failed", "ru") in text and tool._t("rejected", "ru") not in text


def test_hist072_refused_install_does_not_say_installing(monkeypatch):
    run, tool = _assistant(monkeypatch, {"running": False}, started=False, background=("model list unavailable", 6, False))
    result = run("install")
    assert not result.success and "model list unavailable" in result.text
    run, tool = _assistant(monkeypatch, {"running": False}, started=False, background=("pulling", None, True))
    assert run("install").text == tool._t("install_busy", "ru")


# HIST-053 ------------------------------------------------------------------------------
def _audio_with_broken_core(monkeypatch, keys):
    from jarvis.tools.domains import audio
    monkeypatch.setattr(audio.os, "name", "nt")
    config = SimpleNamespace(get=lambda key, default=None: default)
    handler = _registered(audio.register_audio_tools, config=config, send_media_key=keys.append,
                          run_command=lambda *_a, **_k: None)["audio"]
    import jarvis.tools.domains.audio as module
    return handler, module


def test_hist053_zero_step_and_mute_fallback_never_guess(monkeypatch):
    keys = []
    handler, module = _audio_with_broken_core(monkeypatch, keys)
    source = __import__("inspect").getsource(module.register_audio_tools)
    assert "_send_media_key(0xAD)" not in source            # no blind mute toggle any more
    assert 'if delta == 0:' in source


# HIST-028 ------------------------------------------------------------------------------
def test_hist028_named_browser_is_never_widened_to_another_browser(monkeypatch):
    from jarvis import browser_control as bc
    scopes = []
    monkeypatch.setattr(bc.os, "name", "nt")
    monkeypatch.setattr(bc, "find_service_tab_passive", lambda service, browser: scopes.append(("tab", browser)) or None)
    monkeypatch.setattr(bc, "_find_youtube_tab_from_live_sessions", lambda browser: scopes.append(("session", browser)) or None)
    monkeypatch.setattr(bc, "_control_unique_browser_media_session", lambda action, browser, seconds: scopes.append(("cold", browser)) or None)
    with pytest.raises(RuntimeError):
        bc.control_service_media("pause", "youtube", "firefox")
    assert scopes and all(scope == "firefox" for _kind, scope in scopes)
    scopes.clear()
    with pytest.raises(RuntimeError):
        bc.control_service_media("pause", "youtube", "")
    assert ("tab", "") in scopes   # an unnamed request may look at any open browser


# HIST-029 ------------------------------------------------------------------------------
def test_hist029_gpu_average_does_not_mix_two_devices(monkeypatch):
    from jarvis.runtime import hardware
    shots = iter([
        hardware.HardwareSnapshot(timestamp=1, cpu_percent=10, cpu_freq_mhz=1, cpu_temp_c=None, cpu_power_w=None, ram_percent=1, ram_used_gb=1, ram_total_gb=2,
                                  gpu_name="GPU A", gpu_temp_c=80.0),
        hardware.HardwareSnapshot(timestamp=2, cpu_percent=20, cpu_freq_mhz=1, cpu_temp_c=None, cpu_power_w=None, ram_percent=1, ram_used_gb=1, ram_total_gb=2,
                                  gpu_name="GPU B", gpu_temp_c=40.0),
    ])
    monkeypatch.setattr(hardware, "snapshot", lambda: next(shots))
    data = hardware.average(2, interval=0)
    assert data["gpu_name"] == "GPU B" and data["avg_gpu_temp_c"] == 40.0       # not (80+40)/2 under B's name
    assert data["avg_gpu_device_changed"] is True and data["avg_cpu_percent"] == 15.0


# HIST-052 ------------------------------------------------------------------------------
def test_hist052_explicit_microphone_is_not_replaced_by_the_default(monkeypatch):
    from jarvis.audio.input_controller import AudioInputController
    ctl = AudioInputController()
    calls = []
    ctl.native = SimpleNamespace(available=lambda: False, name="core")
    ctl.fallback = SimpleNamespace(name="ps", set_volume=lambda *a, **k: calls.append("fallback") or 30,
                                   get_volume=lambda *a, **k: 30, set_mute=lambda *a, **k: True)
    for operation in (lambda: ctl.set_volume(30, device_id="{mic-A}"), lambda: ctl.get_volume(device_id="{mic-A}"),
                      lambda: ctl.set_mute(True, device_id="{mic-A}")):
        with pytest.raises(RuntimeError):
            operation()
    assert calls == []
    assert ctl.set_volume(30)["provider"] == "ps"     # an unnamed request may use the default device


# NEO-RELIABILITY-001 -------------------------------------------------------------------
def test_reliability001_interrupted_stream_keeps_text_and_says_it_may_be_incomplete():
    from jarvis.runtime import local_chat169 as lc
    lc.reset_availability()

    class Ollama:
        def local_chat_enabled(self):
            return True

        def chat_text(self, messages, on_delta=None):
            on_delta("Первая часть. ")
            on_delta("Вторая часть.")
            raise ConnectionError("model went away")
    shown = []
    config = SimpleNamespace(get=lambda key, default=None: default)
    reply = lc.answer(Ollama(), config, "расскажи о берёзе", "ru", scope="t187", on_delta=shown.append)
    assert reply.startswith("Первая часть. Вторая часть.") and reply.endswith(lc._INCOMPLETE187["ru"])
    assert getattr(reply, "incomplete", False) is True
    assert shown[-1].strip() == lc._INCOMPLETE187["ru"]


def test_reliability001_cancel_and_full_answer_are_not_marked():
    from jarvis.runtime import local_chat169 as lc
    from jarvis.runtime.cancellation import OperationCancelled
    lc.reset_availability()

    class Stopped:
        def local_chat_enabled(self):
            return True

        def chat_text(self, messages, on_delta=None):
            on_delta("Часть")
            raise OperationCancelled()

    class Full:
        def local_chat_enabled(self):
            return True

        def chat_text(self, messages, on_delta=None):
            on_delta("Готовый ответ.")
            return "Готовый ответ."
    config = SimpleNamespace(get=lambda key, default=None: default)
    with pytest.raises(OperationCancelled):
        lc.answer(Stopped(), config, "q", "ru", scope="t187b", on_delta=lambda p: None)
    full = lc.answer(Full(), config, "q", "ru", scope="t187c", on_delta=lambda p: None)
    assert full == "Готовый ответ." and not getattr(full, "incomplete", False)
