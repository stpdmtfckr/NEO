"""Download framing through real requests/urllib3/http.client, without sockets."""
from types import SimpleNamespace
from unittest.mock import patch
import gzip
import http.client
import io
import json

import pytest
import requests
import urllib3
from urllib3.exceptions import DecodeError, ProtocolError, ReadTimeoutError, SSLError

import jarvis.model_management as mm
from jarvis.testing.isolation import external_io_blocked


class Source(io.RawIOBase):
    def __init__(self, segments, callback=None):
        self.segments = iter(segments)
        self.pending = b""
        self.index = -1
        self.callback = callback
        self.wire_bytes = 0

    def readable(self):
        return True

    def readinto(self, target):
        if not self.pending:
            try:
                self.pending = next(self.segments)
            except StopIteration:
                return 0
            self.index += 1
            if self.index and self.callback:
                self.callback(self.index)
        count = min(len(target), len(self.pending))
        target[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        self.wire_bytes += count
        return count


class Raw(urllib3.response.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.available_calls = []
        self.fallback_calls = []
        self.available_bytes = 0
        self.release_calls = 0

    def read1(self, amt=None, decode_content=None):
        self.available_calls.append((amt, decode_content))
        value = super().read1(amt, decode_content=decode_content)
        self.available_bytes += len(value)
        return value

    def stream(self, amt=None, decode_content=None):
        self.fallback_calls.append((amt, decode_content))
        yield from super().stream(amt, decode_content=decode_content)

    def release_conn(self):
        self.release_calls += 1
        return super().release_conn()


def response(packets, *, chunked=True, encoding=None, callback=None,
             truncated=False, capability="read1"):
    header = b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
    if chunked:
        header += b"Transfer-Encoding: chunked\r\n"
    else:
        size = sum(map(len, packets)) + (1 if truncated else 0)
        header += f"Content-Length: {size}\r\n".encode()
    if encoding:
        header += b"Content-Encoding: " + encoding.encode() + b"\r\n"
    segments = [header + b"\r\n"]
    segments.extend(f"{len(p):x}\r\n".encode() + p + b"\r\n" if chunked else p for p in packets)
    if chunked and not truncated:
        segments.append(b"0\r\n\r\n")
    source = Source(segments, callback)
    buffered = io.BufferedReader(source)
    std = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *a: buffered), method="POST")
    std.begin()
    raw = Raw(body=std, headers=dict(std.getheaders()), status=std.status,
              preload_content=False, decode_content=False, original_response=std)
    if capability == "missing":
        raw.read1 = None
    elif capability == "plain":
        def plain_read1(amt=None):
            raise AssertionError("A read1 without decode_content must not run")
        raw.read1 = plain_read1
    result = requests.Response()
    result.status_code = 200
    result.raw = raw
    result.iter_lines = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("No iter_lines"))
    return result, source


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def pieces(value, width):
    return [value[i:i + width] for i in range(0, len(value), width)]


@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():
        yield


@pytest.fixture
def op():
    return mm.ModelOperation("download", "inert:1b", "http://127.0.0.1:11434")


def pull(op, result):
    op._session = SimpleNamespace(post=lambda *a, **kw: result)
    op._pull()


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("capability", ["read1", "missing", "plain"])
@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_real_framing_unicode_split_and_decompression(op, chunked, capability, encoding):
    payload = b"\xef\xbb\xbf" + wire({"status": "загрузка 😀 你好", "total": 100, "completed": 25}) + b"\r\n"
    payload += wire({"status": "success"})  # Complete final record without newline.
    if encoding:
        payload = gzip.compress(payload)
    result, _ = response(pieces(payload, 3), chunked=chunked, capability=capability, encoding=encoding)
    seen = []
    op.progress.connect(lambda *args: seen.append(args))
    pull(op, result)
    assert seen[0] == ("inert:1b: загрузка 😀 你好 (25%)", 25)
    assert seen[-1] == ("inert:1b: success", -1)
    assert result._content_consumed and result.raw.release_calls > 0
    if capability == "read1":
        assert result.raw.available_calls and set(result.raw.available_calls) == {(4096, True)}
        assert not result.raw.fallback_calls
    else:
        assert result.raw.fallback_calls == [(1, True)]
        assert not result.raw.available_calls


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("capability", ["read1", "missing"])
@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_one_byte_drip_observes_control_without_waiting_to_fill(op, chunked, capability, control):
    now = [100.0]
    def arrived(index):
        if index == 3:
            if control == "cancel":
                op.cancel()
            else:
                now[0] = 2000.0
    result, source = response([b"x"] * 400, chunked=chunked, callback=arrived, capability=capability)
    with patch.object(mm.time, "monotonic", side_effect=lambda: now[0]):
        with pytest.raises(InterruptedError if control == "cancel" else TimeoutError):
            pull(op, result)
    assert source.index == 3
    assert result.raw.release_calls > 0
    if capability == "read1":
        assert result.raw.available_bytes == 3 and len(result.raw.available_calls) == 3


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("control", ["cancel", "deadline", "oversize"])
def test_exact_400_packets_through_real_read1(op, chunked, control):
    now = [100.0]
    def arrived(index):
        if index == 1:
            if control == "cancel":
                op.cancel()
            elif control == "deadline":
                now[0] = 2000.0
    result, source = response([b"x" * 1024] * 400, chunked=chunked, callback=arrived)
    with patch.object(mm.time, "monotonic", side_effect=lambda: now[0]):
        with pytest.raises(InterruptedError if control == "cancel" else TimeoutError if control == "deadline" else ValueError):
            pull(op, result)
    expected = 65 if control == "oversize" else 1
    assert source.index == expected and len(result.raw.available_calls) == expected
    assert result.raw.available_bytes == expected * 1024
    assert result.raw.release_calls > 0


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_decoded_size_cap_and_bounded_read_requests(op, chunked, encoding):
    payload = b"x" * 65537
    if encoding:
        payload = gzip.compress(payload)
    result, _ = response([payload], chunked=chunked, encoding=encoding)
    with pytest.raises(ValueError, match="длинную строку"):
        pull(op, result)
    assert result.raw.available_bytes == 65537
    assert len(result.raw.available_calls) == 17
    assert set(result.raw.available_calls) == {(4096, True)}
    assert result.raw.release_calls > 0


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r", b""])
def test_exact_cap_crlf_and_final_record(op, ending):
    base = wire({"status": "success", "padding": ""})
    payload = wire({"status": "success", "padding": "x" * (65536 - len(base))})
    result, _ = response(pieces(payload + ending, 4096))
    pull(op, result)
    assert result.raw.available_bytes == len(payload + ending)


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("capability", ["read1", "missing"])
def test_truncated_transport_keeps_requests_error_and_closes(op, chunked, capability):
    result, _ = response([wire({"status": "pulling"}) + b"\n"],
                         chunked=chunked, truncated=True, capability=capability)
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        pull(op, result)
    assert result.raw.release_calls > 0


@pytest.mark.parametrize("payload", [b"{", b"[]\n", b'{"status":"\xff"}\n', b"{bad}\n"])
def test_bad_records_through_read1_fail_clearly(op, payload):
    result, _ = response(pieces(payload, 2))
    with pytest.raises(ValueError, match="Неверный ответ загрузки Ollama"):
        pull(op, result)
    assert result.raw.release_calls > 0


def test_invalid_compressed_stream_uses_requests_error(op):
    result, _ = response([b"not gzip data"], encoding="gzip")
    with pytest.raises(requests.exceptions.ContentDecodingError):
        pull(op, result)
    assert result.raw.release_calls > 0


@pytest.mark.parametrize("cause,expected", [
    (ProtocolError("truncated"), requests.exceptions.ChunkedEncodingError),
    (DecodeError("bad encoding"), requests.exceptions.ContentDecodingError),
    (ReadTimeoutError(None, None, "inert timeout"), requests.exceptions.ConnectionError),
    (SSLError("inert tls"), requests.exceptions.SSLError),
])
@pytest.mark.parametrize("control", [None, "cancel", "deadline"])
def test_raw_errors_preserve_requests_contract_and_control_precedence(op, cause, expected, control):
    result, _ = response([b"x"])
    now = [100.0]
    calls = []
    def read1(amt, decode_content=None):
        calls.append((amt, decode_content))
        if control == "cancel":
            op.cancel()
        elif control == "deadline":
            now[0] = 2000.0
        raise cause
    result.raw.read1 = read1
    with patch.object(mm.time, "monotonic", side_effect=lambda: now[0]):
        with pytest.raises(InterruptedError if control == "cancel" else TimeoutError if control == "deadline" else expected):
            pull(op, result)
    assert calls == [(4096, True)] and result.raw.release_calls > 0


def test_capability_type_error_after_read_is_not_retried(op):
    result, _ = response([b"x"])
    calls = []
    def read1(amt, decode_content=None):
        calls.append((amt, decode_content))
        raise TypeError("adapter failed after reading")
    result.raw.read1 = read1
    with pytest.raises(TypeError, match="after reading"):
        pull(op, result)
    assert calls == [(4096, True)] and not result.raw.fallback_calls
    assert result.raw.release_calls > 0


def test_uninspectable_capability_uses_fallback_before_any_read(op):
    result, _ = response([wire({"status": "success"})])
    with patch.object(mm.inspect, "signature", side_effect=ValueError("uninspectable")):
        pull(op, result)
    assert result.raw.fallback_calls == [(1, True)] and not result.raw.available_calls


def test_consumed_response_uses_cached_content(op):
    result, _ = response([b"must not read transport"])
    result._content = wire({"status": "success"})
    result._content_consumed = True
    pull(op, result)
    assert not result.raw.available_calls and not result.raw.fallback_calls


def test_cancel_callback_between_records_in_one_available_chunk(op):
    result, source = response([wire({"status": "pulling"}) + b"\n" + wire({"status": "success"})])
    seen = []
    def cancel(*args):
        seen.append(args)
        op.cancel()
    op.progress.connect(cancel)
    with pytest.raises(InterruptedError):
        pull(op, result)
    assert len(seen) == 1 and source.index == 1 and len(result.raw.available_calls) == 1
    assert result.raw.release_calls > 0
