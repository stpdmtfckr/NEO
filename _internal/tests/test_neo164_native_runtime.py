"""Source/native distribution boundaries; no Windows execution claims.

NEO 179: the isolated Piper runtime is gone with speech output; the host and its
private-interpreter switch stay generic.
"""
from jarvis.runtime import distribution164


def test_native_worker_with_a_private_interpreter_uses_the_host(tmp_path, monkeypatch):
    monkeypatch.setenv('NEO_HOST_EXECUTABLE', str(tmp_path/'Application/NEO.exe'))
    monkeypatch.setenv('NEO_PACKAGE_ROOT', str(tmp_path/'Package'))
    private = tmp_path/'runtimes/example/Scripts/python.exe'
    command = distribution164.worker_command('jarvis.stt_worker', python=private)
    assert command == [str(tmp_path/'Application/NEO.exe'), '--neo-runtime',
                       str(tmp_path/'runtimes/example'), '-u', '-m', 'jarvis.stt_worker']


def test_host_environment_alone_cannot_switch_source_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('NEO_HOST_EXECUTABLE', str(tmp_path/'NEO.exe'))
    monkeypatch.delenv('NEO_PACKAGE_ROOT', raising=False)
    assert distribution164.native_host() is None


def test_speech_output_workers_are_gone():
    import neo_entry164
    assert not any('speech_out' in name for name in neo_entry164.WORKERS | neo_entry164.HELPERS)
