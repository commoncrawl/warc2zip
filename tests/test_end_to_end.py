"""End-to-end conversion over a synthetic WARC built in-process (no network).

Covers both output formats and asserts the shipped CSVs parse with a stock csv.reader — the
property that a single NUL byte from the wire used to break for a whole crawl.
"""

import csv
import io
import json
import zipfile

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from warc2zip import main

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

            body = b"fetchTimeMs: 42\r\ncharset-detected: utf-8\x00\r\n"
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


def csv_members(zf):
    return [n for n in zf.namelist() if n.endswith(".csv")]


@pytest.mark.parametrize("output_format", ["flat", "sidecar"])
def test_conversion_produces_parseable_csvs(warc_path, tmp_path, output_format):
    out = tmp_path / f"{output_format}.zip"

    skipped = main(str(warc_path), str(out), output_format=output_format)

    assert skipped == 0
    with zipfile.ZipFile(out) as zf:
        members = csv_members(zf)
        assert len(members) == 8

        for name in members:
            content = zf.read(name).decode("utf-8")
            assert "\x00" not in content, f"{name} still carries a NUL"
            rows = list(csv.reader(io.StringIO(content)))  # raises on NUL / unbalanced quotes
            assert all(len(row) == len(rows[0]) for row in rows), f"{name} has ragged rows"

        manifest = [
            json.loads(line)
            for line in zf.read(next(n for n in zf.namelist() if n.endswith("manifest.jsonl")))
            .decode("utf-8")
            .splitlines()
        ]
        assert len(manifest) == len(CAPTURES)
        assert {entry["warc_target_uri"] for entry in manifest} == {uri for uri, _, _, _ in CAPTURES}


def test_nul_from_the_wire_is_escaped_in_the_csv(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        name = next(n for n in zf.namelist() if n.endswith("response_http_headers.csv"))
        rows = list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))

    assert ["connection", "close\\x00"] in [row[1:] for row in rows]


def test_payloads_and_metadata_are_present(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        payloads = [n for n in zf.namelist() if n.endswith((".html", ".json"))]
        assert len(payloads) == len(CAPTURES)

        request_csv = next(n for n in zf.namelist() if n.endswith("request_warc_headers.csv"))
        metadata_csv = next(n for n in zf.namelist() if n.endswith("metadata.csv"))
        assert len(list(csv.reader(io.StringIO(zf.read(request_csv).decode("utf-8"))))) > 1
        assert len(list(csv.reader(io.StringIO(zf.read(metadata_csv).decode("utf-8"))))) > 1


def test_sidecar_format_writes_per_capture_files(warc_path, tmp_path):
    out = tmp_path / "sidecar.zip"
    main(str(warc_path), str(out), output_format="sidecar")

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert any(n.endswith(".response.http") for n in names)
        assert any(n.endswith(".request.warc") for n in names)
        assert any(n.endswith(".metadata.warc-fields") for n in names)
        assert any("/example.com/" in n for n in names)
