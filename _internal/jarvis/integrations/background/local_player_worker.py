# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Headless Qt Multimedia worker used by NEO JARVIS background playback.

The worker owns QMediaPlayer in its own process.  Commands arrive over a
loopback-only TCP socket, so JARVIS never needs to focus a window or synthesize
mouse/keyboard input to play a Yandex Music stream.
"""

import argparse
import json
import os
import socketserver
import sys
import threading
from typing import Any

from jarvis.runtime.media_levels import spectrum_from_samples

from PySide6.QtCore import QObject, QUrl, Signal, Slot, QCoreApplication
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
try:
    from PySide6.QtMultimedia import QMediaDevices
except ImportError:  # pragma: no cover - compatibility with older user runtimes
    QMediaDevices = None  # type: ignore[assignment]
try:
    # Qt 6.8+: decoded PCM tap for a real music-reactive visualizer.
    from PySide6.QtMultimedia import QAudioBufferOutput
except ImportError:  # pragma: no cover - compatibility with older user runtimes
    QAudioBufferOutput = None  # type: ignore[assignment]


class PlayerBridge(QObject):
    command = Signal(object)

    def __init__(self, app: QCoreApplication) -> None:
        super().__init__()
        self.app = app
        self.audio = QAudioOutput()
        self.audio.setVolume(1.0)
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "state": "stopped",
            "media_status": "no_media",
            "title": "",
            "artist": "",
            "url": "",
            "error": "",
            "error_kind": "",
            "seekable": False,
            "buffer_progress": 0.0,
            "volume": 1.0,
            "position_ms": 0,
            "duration_ms": 0,
            "levels": [],
            "audio_levels_real": False,
            "audio_levels_kind": "",
            "output_device": "",
        }
        self.audio_buffer_output = None
        if QAudioBufferOutput is not None:
            try:
                self.audio_buffer_output = QAudioBufferOutput(self)
                self.player.setAudioBufferOutput(self.audio_buffer_output)
                self.audio_buffer_output.audioBufferReceived.connect(self._on_audio_buffer)
                self._state["audio_levels_real"] = True
                self._state["audio_levels_kind"] = "spectrum"
            except Exception:
                # The FFmpeg backend is required for QAudioBufferOutput. Keep
                # playback working and let the UI use its smooth fallback if a
                # platform/backend does not expose decoded buffers.
                self.audio_buffer_output = None
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(self._on_error)
        self.player.seekableChanged.connect(self._on_seekable)
        self.player.bufferProgressChanged.connect(self._on_buffer_progress)
        self.command.connect(self._handle)

    @staticmethod
    def _enum_name(value: Any) -> str:
        return str(getattr(value, "name", value)).casefold()

    def _on_state(self, state: Any) -> None:
        name = self._enum_name(state)
        if "playing" in name:
            value = "playing"
        elif "paused" in name:
            value = "paused"
        else:
            value = "stopped"
        with self._lock:
            self._state["state"] = value
            if value != "playing":
                self._state["levels"] = []
                self._state["audio_levels_kind"] = ""

    def _on_audio_buffer(self, buffer: Any) -> None:
        """Extract real logarithmic frequency bands from decoded PCM.

        Qt exposes samples in the source format, so use normalizedSampleValue for
        a bounded contiguous tail and run one small FFT. This keeps every bar tied
        to a distinct frequency range while avoiding expensive per-frame DSP in
        the GUI process.
        """
        try:
            if buffer is None or not buffer.isValid():
                with self._lock:
                    self._state["levels"] = []
                    self._state["audio_levels_real"] = False
                    self._state["audio_levels_kind"] = ""
                return
            fmt = buffer.format()
            channels = max(1, int(fmt.channelCount() or 1))
            bytes_per_sample = max(1, int(fmt.bytesPerSample() or 1))
            frame_bytes = bytes_per_sample * channels
            raw = buffer.constData()
            raw_size = len(raw)
            frame_count = min(int(buffer.frameCount() or 0), raw_size // frame_bytes)
            if frame_count <= 0:
                return
            # A contiguous window is required for a real frequency transform.
            # 2048 frames caps work while retaining useful low-frequency detail.
            take = min(2048, frame_count)
            first = max(0, frame_count - take)
            mono: list[float] = []
            append = mono.append
            for frame_index in range(first, frame_count):
                base = frame_index * frame_bytes
                total = 0.0
                for channel in range(channels):
                    offset = base + channel * bytes_per_sample
                    total += float(fmt.normalizedSampleValue(raw[offset:]))
                append(total / channels)
            levels = spectrum_from_samples(
                mono, max(1, int(fmt.sampleRate() or 48000)), width=48
            )
            with self._lock:
                self._state["levels"] = levels
                self._state["audio_levels_real"] = bool(levels)
                self._state["audio_levels_kind"] = "spectrum" if levels else ""
        except Exception:
            # Never let visualizer analysis interrupt music playback.
            with self._lock:
                self._state["levels"] = []
                self._state["audio_levels_real"] = False
                self._state["audio_levels_kind"] = ""

    def _on_media_status(self, status: Any) -> None:
        with self._lock:
            self._state["media_status"] = self._enum_name(status)

    def _on_position(self, position: int) -> None:
        with self._lock:
            self._state["position_ms"] = int(position or 0)

    def _on_duration(self, duration: int) -> None:
        with self._lock:
            self._state["duration_ms"] = int(duration or 0)

    def _on_seekable(self, value: bool) -> None:
        with self._lock:
            self._state["seekable"] = bool(value)

    def _on_buffer_progress(self, value: float) -> None:
        with self._lock:
            self._state["buffer_progress"] = max(0.0, min(1.0, float(value)))

    def _on_error(self, _error: Any, error_string: str = "") -> None:
        name = self._enum_name(_error)
        text = str(error_string or self.player.errorString() or "Qt Multimedia playback error")
        with self._lock:
            self._state["error"] = "" if name == "noerror" else text
            self._state["error_kind"] = "network" if "network" in name else name

    def _refresh_output_device(self) -> dict[str, Any]:
        """Rebind Qt audio to the current Windows default output endpoint.

        QAudioOutput may keep the device selected when the worker was created.
        Rebinding is deliberately in-process so the current source, position,
        queue ownership and volume survive a Windows default-device switch.
        """
        if QMediaDevices is None or not hasattr(self.audio, "setDevice"):
            raise RuntimeError("Qt Multimedia не поддерживает динамическое переключение устройства вывода")
        device = QMediaDevices.defaultAudioOutput()
        if device is None:
            raise RuntimeError("Qt Multimedia не вернул устройство вывода по умолчанию")
        self.audio.setDevice(device)
        description = ""
        try:
            description = str(device.description() or "")
        except Exception:
            pass
        with self._lock:
            self._state["output_device"] = description
        return {"output_device": description, "rebound": True}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    @Slot(object)
    def _handle(self, packet: dict[str, Any]) -> None:
        action = str(packet.get("action", "") or "").strip().casefold()
        response = packet.get("response")
        done = packet.get("done")
        result: dict[str, Any] = {"ok": True, "action": action, "state_before": self.snapshot().get("state", "")}
        try:
            if action == "play":
                url = str(packet.get("url", "") or "").strip()
                if not url:
                    raise ValueError("Empty media URL")
                with self._lock:
                    # A new QMediaPlayer source may emit its state signals a little
                    # later. Reset old timing/status immediately so the controller
                    # cannot observe the previous track's near-end position while
                    # the new one is already reported as playing.
                    self._state.update({
                        "state": "stopped",
                        "media_status": "loading_media",
                        "title": str(packet.get("title", "") or ""),
                        "artist": str(packet.get("artist", "") or ""),
                        "url": url,
                        "error": "",
                        "error_kind": "",
                        "seekable": False,
                        "buffer_progress": 0.0,
                        "position_ms": 0,
                        "duration_ms": 0,
                        "levels": [],
                        "audio_levels_kind": "",
                    })
                source = QUrl.fromLocalFile(os.path.abspath(url)) if os.path.isfile(url) else QUrl(url)
                # Qt ignores setSource with the existing URL. After a broken
                # HTTP stream that would replay the same failed demuxer forever.
                # Detach it first so recovery really opens a new connection.
                if self.player.source() == source:
                    self.player.setSource(QUrl())
                self.player.setSource(source)
                self.player.play()
            elif action == "pause":
                self.player.pause()
            elif action in {"resume", "play_resume"}:
                self.player.play()
            elif action == "stop":
                self.player.stop()
            elif action == "seek":
                if not self.player.isSeekable():
                    result.update({"seek_applied": False, "reason": "source_not_seekable"})
                    position = None
                else:
                    position = max(0, int(packet.get("position_ms", 0)))
                duration = int(self.player.duration() or 0)
                if position is not None:
                    if duration > 0:
                        position = min(position, max(0, duration - 500))
                    self.player.setPosition(position)
                    result.update({"seek_applied": True, "requested_position_ms": position})
            elif action == "set_volume":
                volume = max(0.0, min(1.0, float(packet.get("volume", 1.0))))
                self.audio.setVolume(volume)
                with self._lock:
                    self._state["volume"] = volume
                result["volume"] = volume
            elif action in {"refresh_output", "rebind_output"}:
                result.update(self._refresh_output_device())
            elif action == "shutdown":
                self.player.stop()
                self.app.quit()
            elif action == "status":
                pass
            else:
                raise ValueError(f"Unknown background player action: {action}")
            result.update(self.snapshot())
        except Exception as exc:
            result = {"ok": False, "action": action, "error": str(exc), **self.snapshot()}
        if isinstance(response, dict):
            response.update(result)
        if hasattr(done, "set"):
            done.set()


class CommandHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(1024 * 1024)
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            response: dict[str, Any] = {}
            done = threading.Event()
            request["response"] = response
            request["done"] = done
            self.server.bridge.command.emit(request)  # type: ignore[attr-defined]
            if not done.wait(3.0):
                response = {"ok": False, "error": "Qt player command timed out"}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bridge: PlayerBridge) -> None:
        super().__init__(address, CommandHandler)
        self.bridge = bridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-lifeline", action="store_true")
    args = parser.parse_args()
    if args.parent_lifeline:
        def watch_parent() -> None:
            # Only this process is retired. The parent owns the sole pipe writer;
            # its crash/forced test timeout closes that writer even if Qt is stuck.
            try:
                while sys.stdin.buffer.read(1):
                    pass
            finally:
                os._exit(0)
        threading.Thread(target=watch_parent, name="NEO-PlayerParentLifeline", daemon=True).start()
    app = QCoreApplication([])
    bridge = PlayerBridge(app)
    server = LoopbackServer(("127.0.0.1", args.port), bridge)
    thread = threading.Thread(target=server.serve_forever, name="jarvis-local-player-ipc", daemon=True)
    thread.start()
    try:
        return int(app.exec())
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
