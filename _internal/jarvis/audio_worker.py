# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import queue
import sys
import threading
from typing import Any

from jarvis.audio_backend import _audio_worker_main, _input_devices


def _write(payload: dict[str, Any]) -> None:
    encoded = dict(payload)
    audio = encoded.pop("audio", None)
    if isinstance(audio, (bytes, bytearray, memoryview)):
        encoded["audio_b64"] = base64.b64encode(bytes(audio)).decode("ascii")
    sys.stdout.write(json.dumps(encoded, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _list_devices() -> int:
    try:
        import sounddevice as sd

        devices = _input_devices(sd)
        _write({"ok": True, "devices": devices})
        return 0
    except BaseException as exc:
        _write({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


def main() -> int:
    if "--list-devices" in sys.argv[1:]:
        return _list_devices()

    command_queue: queue.Queue[Any] = queue.Queue(maxsize=32)
    event_queue: queue.Queue[Any] = queue.Queue(maxsize=96)

    def read_commands() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    command_queue.put(payload, timeout=1.0)
                except queue.Full:
                    continue
        finally:
            try:
                command_queue.put({"cmd": "shutdown"}, timeout=0.5)
            except queue.Full:
                pass

    def write_events() -> None:
        # Block until the audio backend actually has an event. The previous
        # 200 ms timeout woke this otherwise idle thread five times per second
        # for the entire lifetime of permanent microphone monitoring.
        while True:
            payload = event_queue.get()
            if payload is None:
                return
            if isinstance(payload, dict):
                _write(payload)

    reader = threading.Thread(target=read_commands, name="NeoJarvis-Audio-stdin", daemon=True)
    writer = threading.Thread(target=write_events, name="NeoJarvis-Audio-stdout", daemon=True)
    reader.start()
    writer.start()
    try:
        _audio_worker_main(command_queue, event_queue)
        return 0
    finally:
        try:
            event_queue.put(None, timeout=0.5)
        except queue.Full:
            pass
        writer.join(timeout=1.0)


if __name__ == "__main__":
    raise SystemExit(main())
