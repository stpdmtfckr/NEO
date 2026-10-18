"""Bounded child-process integration probe for a severed local HTTP audio stream.

Uses only generated audio and loopback HTTP, never an account or external CDN.
The caller supplies the whole-process timeout because a native demuxer can wedge.
"""
from __future__ import annotations
import io
import json
import math
import socket
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from jarvis.integrations.background.local_player import BackgroundLocalPlayer


def main():
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(i * .1))) for i in range(320000)))
    body = raw.getvalue()
    healthy = threading.Event()
    cut = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            online = healthy.is_set()
            requests.append(online)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body if online else body[:160044])
                self.wfile.flush()
                if not online:
                    time.sleep(1.5)
                    cut.set()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    player = BackgroundLocalPlayer()
    url = f'http://127.0.0.1:{server.server_port}/generated.wav'
    result = {}
    try:
        player.set_volume(0)
        try:
            initial = player.play(url, title='Generated stream')
        except (OSError, RuntimeError) as exc:
            initial = {'initial_exception': type(exc).__name__}
        first_process = player._process
        assert cut.wait(3), 'The server did not exercise a real disconnect'
        healthy.set()
        time.sleep(.5)
        try:
            player.set_volume(0)
            recovered = player.play(url, title='Generated stream')
        except (OSError, RuntimeError):
            recovered = {}
        restarted = False
        if not recovered.get('timeline_verified'):
            restarted = player.recreate_if_unresponsive()
            player.set_volume(0)
            recovered = player.play(url, title='Generated stream')
        assert recovered.get('timeline_verified'), recovered
        assert int(recovered.get('position_ms') or 0) > 0, recovered
        assert requests[0] is False and True in requests, requests
        if restarted:
            assert player._process is not first_process
        result = {'cut_http_stream': True, 'recovered_timeline': True,
                  'recreated_owned_worker': restarted, 'initial_verified': bool(initial.get('timeline_verified'))}
    finally:
        player.close()
        server.shutdown()
        server.server_close()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
