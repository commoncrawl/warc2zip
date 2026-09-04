"""Shared fixtures: the synthetic three-capture WARC used by the end-to-end and --fetch tests."""

import io

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
