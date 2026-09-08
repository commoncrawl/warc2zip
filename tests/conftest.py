"""Shared fixtures: the synthetic three-capture WARC used by the end-to-end and --fetch tests."""

import io
from pathlib import Path

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

# The shape CC writes into a metadata record's warc-fields body.
CLD2 = '{"reliable":true,"languages":[{"code":"zh","text-covered":0.87,"name":"Chinese"}]}'

CAPTURES = [
    (
        "https://example.com/",
        "text/html",
        b"<html><body>hello</body></html>",
        [("Content-Type", "text/html; charset=UTF-8"), ("Connection", "close\x00")],
    ),
    (
        "https://cloudflare-ish.example.org/index.html",
        "text/html",
        b"<html>report-to</html>",
        [
            ("Content-Type", "text/html"),
            ("Report-To", '{"group":"cf-nel","max_age":604800}'),
            ("Server-Timing", 'cfCacheStatus;desc="DYNAMIC"'),
            ("Cache-Control", "no-store, must-revalidate, no-cache"),
        ],
    ),
    (
        "https://plain.example.net/data.json",
        "application/json",
        b'{"ok": true}',
        [("Content-Type", "application/json"), ("X-Fold", "a\r\n b")],
    ),
]


@pytest.fixture
def warc_path(tmp_path):
    """A three-capture WARC: warcinfo + response/request/metadata per capture."""
    path = tmp_path / "test.warc.gz"
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(writer.create_warcinfo_record("test.warc.gz", {"software": "warc2zip-tests"}))
        for i, (uri, _mime, payload, headers) in enumerate(CAPTURES):
            request_id = f"<urn:uuid:req-{i}>"

            http_headers = StatusAndHeaders("200 OK", headers, protocol="HTTP/1.1")
            response = writer.create_warc_record(
                uri,
                "response",
                payload=io.BytesIO(payload),
                length=len(payload),
                http_headers=http_headers,
                warc_headers_dict={"WARC-Concurrent-To": request_id},
            )
            writer.write_record(response)
            response_id = response.rec_headers.get_header("WARC-Record-ID")

            request_headers = StatusAndHeaders(
                "GET / HTTP/1.1", [("Host", "example.com"), ("User-Agent", 'cc-bot/1.0 "test"')], is_http_request=True
            )
            writer.write_record(
                writer.create_warc_record(
                    uri,
                    "request",
                    http_headers=request_headers,
                    warc_headers_dict={"WARC-Record-ID": request_id, "WARC-Concurrent-To": response_id},
                )
            )

            body = (
                b"fetchTimeMs: 42\r\n"
                b"charset-detected: utf-8\x00\r\n"
                + f"languages-cld2: {CLD2}\r\n".encode()
                + b"http-header-user-agent: cc-bot/1.0 (X11; Linux)\r\n"
                b"  continued-on-the-next-line\r\n"
            )
            writer.write_record(
                writer.create_warc_record(
                    uri,
                    "metadata",
                    payload=io.BytesIO(body),
                    length=len(body),
                    warc_headers_dict={
                        "WARC-Concurrent-To": response_id,
                        "Content-Type": "application/warc-fields",
                    },
                )
            )
    return path


# --- a stand-in for archive.org ----------------------------------------------------------------
#
# ia:// reads go to https://archive.org/download/<item>/<file>, which 302s to a data node on
# another origin that honours Range. This server reproduces exactly that: /download/... on
# 127.0.0.1 redirects to /items/... on `localhost` (same server, different origin, so aiohttp
# applies its cross-origin rule and drops any Cookie/Authorization *headers*), and the item route
# serves byte ranges — optionally only to requests carrying a login cookie.

import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit


class _IAHandler(BaseHTTPRequestHandler):
    state = None  # set per server: directory, require_cookie, hits

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _serve(self, send_body):
        path = urlsplit(self.path).path
        self.state.hits.append(SimpleNamespace(path=path, headers=dict(self.headers)))
        if path.startswith("/download/"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/items/{path[len('/download/'):]}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith("/items/"):
            self.send_error(404)
            return
        required = self.state.require_cookie
        if required and f"{required[0]}={required[1]}" not in self.headers.get("Cookie", ""):
            self.send_error(403)
            return
        target = self.state.directory / path[len("/items/") :]
        if not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        start, end = 0, len(data) - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            first, _, last = range_header[len("bytes=") :].partition("-")
            start = int(first)
            end = min(int(last), len(data) - 1) if last else len(data) - 1
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        body = data[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)


@pytest.fixture
def ia_server(tmp_path, monkeypatch):
    """A local archive.org: `download_url` and `cookie_domain` of InternetArchiveFileSystem are
    pointed at it for the test. `.add(item, path)` publishes a file; `.require_cookie` gates the
    data-node route; `.hits` records every request."""
    from fsspec.implementations.ia import InternetArchiveFileSystem

    directory = tmp_path / "items"
    directory.mkdir()
    state = SimpleNamespace(directory=directory, require_cookie=None, hits=[])
    handler = type("Handler", (_IAHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def add(item, path):
        (directory / item).mkdir(exist_ok=True)
        shutil.copy(path, directory / item / Path(path).name)
        return f"ia://{item}/{Path(path).name}"

    state.add = add
    state.download_url = f"http://127.0.0.1:{server.server_port}/download/"
    monkeypatch.setattr(InternetArchiveFileSystem, "download_url", state.download_url)
    monkeypatch.setattr(InternetArchiveFileSystem, "cookie_domain", "localhost")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
