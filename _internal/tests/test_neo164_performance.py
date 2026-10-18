"""Measured operation budgets and Windows API contracts, no fake CPU readings."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from jarvis.integrations import bluetooth_devices as bt
from jarvis.runtime import background_cpu164 as cpu
from jarvis.adaptation.ambient import SystemAudioSpeechMonitor
from jarvis.voice import RecordingResult, TranscriptionService


@pytest.fixture
def bluetooth(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bt, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(bt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bt, "_BT_CACHE", (0., []))
    monkeypatch.setattr(bt, "_BT_PNP_CACHE", (0., ""))
    monkeypatch.setattr(bt, "_BT_LOCK", threading.RLock())
    return clock


def test_background_bluetooth_keeps_connection_live_without_restarting_powershell(bluetooth, monkeypatch):
    connected = [True]
    native = Mock(side_effect=lambda: bt._BluetoothRows([
        {"name": "Headset", "connected": connected[0], "battery": None}
    ], available=True))
    shell = Mock(return_value=json.dumps([{"name": "Headset", "connected": True, "battery": 17}]))
    monkeypatch.setattr(bt, "_winrt_bluetooth_devices", native)
    monkeypatch.setattr(bt, "_powershell", shell)
    for i in range(10):
        connected[0] = i % 2 == 0
        row = bt.list_bluetooth_devices(background=True)[0]
        assert row["connected"] == connected[0]  # stale PnP cannot resurrect a connection
        assert row["battery"] == 17
        bluetooth[0] += 30
    assert native.call_count == 10 and shell.call_count == 1
    bt.list_bluetooth_devices(background=True)
    assert shell.call_count == 2
    print("Bluetooth over five minutes: 10 connection refreshes, 1 PowerShell inventory instead of 10")


def test_explicit_bluetooth_request_refreshes_battery_after_background_cache(bluetooth, monkeypatch):
    monkeypatch.setattr(bt, "_winrt_bluetooth_devices", lambda: bt._BluetoothRows([
        {"name": "Headset", "connected": True}], available=True))
    shell = Mock(side_effect=[json.dumps([{"name": "Headset", "battery": 50}]),
                              json.dumps([{"name": "Headset", "battery": 49}])])
    monkeypatch.setattr(bt, "_powershell", shell)
    assert bt.list_bluetooth_devices(background=True)[0]["battery"] == 50
    assert bt.list_bluetooth_devices()[0]["battery"] == 49
    assert bt.list_bluetooth_devices()[0]["battery"] == 49
    assert shell.call_count == 2


def test_bluetooth_successfully_empty_native_inventory_does_not_restore_unpaired_devices(bluetooth, monkeypatch):
    monkeypatch.setattr(bt, "_winrt_bluetooth_devices", lambda: bt._BluetoothRows(available=True))
    shell = Mock(return_value=json.dumps([{"name": "Old speaker", "connected": True}]))
    monkeypatch.setattr(bt, "_powershell", shell)
    assert bt.list_bluetooth_devices() == []


def test_bluetooth_fallback_cache_retries_and_force_bypasses_it(bluetooth, monkeypatch):
    monkeypatch.setattr(bt, "_winrt_bluetooth_devices", lambda: [])
    shell = Mock(return_value='[{"name":"Headset","connected":true}]')
    monkeypatch.setattr(bt, "_powershell", shell)
    bt.list_bluetooth_devices(background=True)
    bluetooth[0] += 30
    bt.list_bluetooth_devices(background=True)
    assert shell.call_count == 1
    bt.list_bluetooth_devices(background=True, force=True)
    assert shell.call_count == 2
    bluetooth[0] += 61
    bt.list_bluetooth_devices(background=True)
    assert shell.call_count == 3


def test_concurrent_bluetooth_requests_share_native_and_pnp_inventory(bluetooth, monkeypatch):
    barrier = threading.Barrier(6)
    def inventory():
        time.sleep(.02)
        return []
    native = Mock(side_effect=inventory)
    shell = Mock(return_value='[{"name":"Headset","connected":true}]')
    monkeypatch.setattr(bt, "_winrt_bluetooth_devices", native)
    monkeypatch.setattr(bt, "_powershell", shell)
    def caller(_):
        barrier.wait(timeout=2)
        return bt.list_bluetooth_devices()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(caller, range(6)))
    assert native.call_count == shell.call_count == 1
    results[0][0]["name"] = "changed"
    assert results[1][0]["name"] == "Headset"


@pytest.mark.parametrize("cores,rate", [(1, 2500), (4, 625), (8, 312), (16, 156), (64, 39)])
def test_background_budget_is_quarter_core_not_quarter_of_machine(cores, rate):
    assert cpu.cpu_rate(.25, cores) == rate
    assert rate / 10000 * cores <= .25


def fake_kernel(monkeypatch, *, set_ok=True, assign_ok=True):
    kernel = SimpleNamespace(CreateJobObjectW=Mock(return_value=42),
                             SetInformationJobObject=Mock(return_value=set_ok),
                             AssignProcessToJobObject=Mock(return_value=assign_ok),
                             CloseHandle=Mock(return_value=True))
    monkeypatch.setattr(cpu, "os", SimpleNamespace(name="nt", cpu_count=lambda: 8))
    monkeypatch.setattr(cpu.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(cpu.ctypes, "get_last_error", lambda: 5, raising=False)
    return kernel


def test_windows_budget_applies_exact_owned_handle_and_releases_job_once(monkeypatch):
    kernel = fake_kernel(monkeypatch)
    process = SimpleNamespace(pid=999, _handle=12345)
    job = cpu.limit_owned_child(process, .25)
    args = kernel.SetInformationJobObject.call_args.args
    assert args[0:2] == (42, 15) and args[3] == 8
    assert args[2]._obj.ControlFlags == 5 and args[2]._obj.CpuRate == 312
    kernel.AssignProcessToJobObject.assert_called_once_with(42, 12345)
    kernel.CloseHandle.assert_not_called()
    job.close()
    job.close()
    kernel.CloseHandle.assert_called_once_with(42)


@pytest.mark.parametrize("set_ok,assign_ok", [(False, True), (True, False)])
def test_rejected_cpu_limit_closes_only_new_job_without_killing_process(monkeypatch, set_ok, assign_ok):
    kernel = fake_kernel(monkeypatch, set_ok=set_ok, assign_ok=assign_ok)
    process = SimpleNamespace(_handle=12345, kill=Mock(), terminate=Mock())
    assert cpu.limit_owned_child(process) is None
    kernel.CloseHandle.assert_called_once_with(42)
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def make_ambient():
    config = SimpleNamespace(get=lambda key, default=None: {
        "ambient_learning.system_audio_transcription_enabled": True,
    }.get(key, default))
    controller = SimpleNamespace(_last_system_audio_process="browser.exe",
                                 governor=SimpleNamespace(allow_heavy=lambda: SimpleNamespace(allow=True)),
                                 observe_system_audio_transcript=Mock())
    return SystemAudioSpeechMonitor(config, controller)


def test_ambient_cap_is_explicit_and_primary_speech_has_no_cap():
    ambient = make_ambient()
    assert ambient._ensure_stt()._background_cpu_cores == .25
    assert ambient._ensure_stt()._process_priority == "below_normal"
    assert TranscriptionService("NeoJarvis-STT-Command")._background_cpu_cores == 0
    assert TranscriptionService("NeoJarvis-STT-Wake")._background_cpu_cores == 0


def test_ambient_decode_failure_backs_off_without_stopping_echo_or_wake(monkeypatch):
    ambient = make_ambient()
    service = Mock()
    service.transcribe.side_effect = [TimeoutError("late"), TimeoutError("late"), "useful transcript"]
    ambient._ensure_stt = Mock(return_value=service)
    clock = [100.0]
    monkeypatch.setattr("jarvis.adaptation.ambient.time.monotonic", lambda: clock[0])
    chunk = RecordingResult(np.ones(32000, dtype=np.float32), 16000)
    ambient._transcribe(chunk)
    for _ in range(4):
        clock[0] += 6
        ambient._transcribe(chunk)
    assert service.transcribe.call_count == 1
    clock[0] = 130
    ambient._transcribe(chunk)
    clock[0] = 185
    ambient._transcribe(chunk)
    assert service.transcribe.call_count == 2
    clock[0] = 190
    ambient._transcribe(chunk)
    assert service.transcribe.call_count == 3
    assert ambient._decode_failures == 0 and ambient._decode_retry_at == 0
    assert not ambient._stop.is_set()
    ambient.controller.observe_system_audio_transcript.assert_called_once_with("useful transcript")


def test_stt_cleanup_closes_job_after_owned_child_has_stopped():
    service = TranscriptionService(background_cpu_cores=.25)
    process = Mock()
    process.poll.side_effect = [None, None]
    service._process = process
    job = Mock()
    job.close.side_effect = lambda: process.wait.assert_called_once_with(timeout=1.5)
    service._background_cpu_job = job
    service._cleanup_locked(force=True)
    job.close.assert_called_once()
    assert service._background_cpu_job is None
