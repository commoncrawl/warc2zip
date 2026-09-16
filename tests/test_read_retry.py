"""Transient failures while reading the input are retried by CountingStream.

The policy is unit-tested with fakes (which errors, where the stream resumes, when it gives
up), then exercised for real: a conversion over a local HTTP server that answers the first
Range request with 503 and cuts the connection halfway through the second. That second case is
the one a per-request retry client would miss — the failure surfaces from the body read, after
the request has already "succeeded".
"""

import functools
import io
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

import warc2zip
from warc2zip import FETCH_DEFAULT_RETRIES, CountingStream, is_retryable, main

PAYLOAD_RE = re.compile(r"^\d+\.[A-Za-z0-9]+$")


class FakeHTTPError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers or {}


class FlakyStream(io.BytesIO):
    """BytesIO whose read() raises the queued errors first — *after* advancing the position,
    the way a partially transferred block leaves a real file object."""

    def __init__(self, data, errors, seekable=True):
        super().__init__(data)
        self.errors = list(errors)
        self._seekable = seekable

    def seekable(self):
        return self._seekable

    def read(self, size=-1):
        if self.errors:
            super().read(7)
            raise self.errors.pop(0)
        return super().read(size)


def read_all(stream, chunk=64):
    out = b""
    while True:
        data = stream.read(chunk)
        if not data:
            return out
        out += data


def test_read_resumes_at_the_byte_count_after_a_transient_error(capsys):
    data = bytes(range(256)) * 4
    sleeps = []
    stream = CountingStream(FlakyStream(data, [FakeHTTPError(503), OSError("reset")]), label="x", sleep=sleeps.append)

    assert read_all(stream) == data  # nothing duplicated, nothing lost
    assert stream.tell() == len(data)
    assert len(sleeps) == 2
    err = capsys.readouterr().err
    assert f"warning: x: attempt 1/{FETCH_DEFAULT_RETRIES + 1} failed (HTTP 503)" in err
    assert f"attempt 2/{FETCH_DEFAULT_RETRIES + 1} failed (reset)" in err


def test_a_pipe_cannot_be_rewound_so_it_is_not_retried():
    sleeps = []
    stream = CountingStream(FlakyStream(b"abc", [FakeHTTPError(503)], seekable=False), sleep=sleeps.append)
    with pytest.raises(FakeHTTPError):
        stream.read(3)
    assert sleeps == []


def test_deterministic_errors_are_raised_at_once():
    sleeps = []
    stream = CountingStream(FlakyStream(b"abc", [PermissionError("403")]), sleep=sleeps.append)
    with pytest.raises(PermissionError):
        stream.read(3)
    assert sleeps == []


def test_gives_up_after_the_configured_retries():
    sleeps = []
    stream = CountingStream(FlakyStream(b"abc", [FakeHTTPError(503)] * 5), retries=3, sleep=sleeps.append)
    with pytest.raises(FakeHTTPError):
        stream.read(3)
    assert len(sleeps) == 3


@pytest.mark.parametrize(
    "exc, expected",
    [
        (FakeHTTPError(503), True),
        (FakeHTTPError(429), True),
        (FakeHTTPError(416), False),
        (FileNotFoundError("404"), False),
        (PermissionError("403"), False),
        (ConnectionResetError(), True),
        (TimeoutError(), True),
    ],
)
def test_is_retryable(exc, expected):
    assert is_retryable(exc) is expected


# --- a conversion over a server that throttles and drops connections ----------------------


def build_warc(path):
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(writer.create_warcinfo_record("flaky.warc.gz", {"software": "warc2zip-tests"}))
        for i in range(3):
            body = f"<html><body>page {i}</body></html>".encode() * 40
            record = writer.create_warc_record(
                f"http://example.com/{i}",
                "response",
                payload=io.BytesIO(body),
                length=len(body),
                http_headers=StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"),
            )
            writer.write_record(record)


class _FaultyRangeHandler(BaseHTTPRequestHandler):
    """Serves `data` with Range support. Each ranged GET consumes one entry of `faults`:
    "503" answers with 503, "drop" sends the headers and half the body then closes."""

    data = b""
    faults = []
    ranged_gets = []

    def log_message(self, *args):
        pass

    def _range(self):
        header = self.headers.get("Range")
        if not header:
            return 0, len(self.data) - 1
        first, _, last = header[len("bytes=") :].partition("-")
        return int(first), min(int(last), len(self.data) - 1) if last else len(self.data) - 1

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        start, end = self._range()
        if start >= len(self.data):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(self.data)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fault = None
        if self.headers.get("Range"):
            fault = self.faults.pop(0) if self.faults else None
            self.ranged_gets.append(fault)
        if fault == "503":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.data[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if fault == "drop":
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)


@pytest.fixture
def faulty_server(tmp_path):
    warc = tmp_path / "flaky.warc.gz"
    build_warc(warc)
    handler = type("Handler", (_FaultyRangeHandler,), {"data": warc.read_bytes(), "faults": [], "ranged_gets": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    handler.url = f"http://127.0.0.1:{server.server_port}/flaky.warc.gz"
    handler.local = warc
    try:
        yield handler
    finally:
        server.shutdown()
        server.server_close()


def payloads(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(zf.read(n) for n in zf.namelist() if PAYLOAD_RE.match(n.rsplit("/", 1)[-1]))


def test_conversion_survives_a_503_and_a_dropped_connection(faulty_server, tmp_path, monkeypatch, capsys):
    faulty_server.faults[:] = ["503", "drop"]
    sleeps = []
    monkeypatch.setattr(warc2zip, "CountingStream", functools.partial(CountingStream, sleep=sleeps.append))

    assert main(faulty_server.url, str(tmp_path / "remote.zip")) == 0
    assert main(str(faulty_server.local), str(tmp_path / "local.zip")) == 0

    assert faulty_server.ranged_gets[:3] == ["503", "drop", None]
    assert len(sleeps) == 2
    err = capsys.readouterr().err
    assert err.count("retrying in") == 2
    assert payloads(tmp_path / "remote.zip") == payloads(tmp_path / "local.zip")
    assert len(payloads(tmp_path / "remote.zip")) == 3
