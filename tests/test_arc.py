"""End-to-end conversion over a synthetic ARC/1.1 built in-process (no network).

ARC is what the pre-2009 crawls are in (Internet Archive / Heritrix 1.x, e.g. the EOT-2004
harvests), and it differs from WARC in three ways that this module has to absorb:

  * no WARC-Record-ID — the format predates record ids entirely, so the grouping dict in main()
    has nothing to key on and every record used to collide under the single key None, leaving
    exactly one row in manifest.csv no matter how many records went in;
  * different header names — a 5-field header line (uri, ip, date, content-type, length) rather
    than named WARC-* headers;
  * records with no HTTP layer at all — dns: lookups are first-class ARC records, so the HTTP
    Content-Type that detect_mime_type() normally leans on simply is not there.

warcio has no ARC writer, so the fixture is assembled by hand. That is a feature: the bytes below
are the format, and a regression that changes how they are read shows up here rather than being
hidden behind a library helper.
"""

import csv
import gzip
import io
import json
import zipfile

import pytest

from warc2zip import extract_crawl_name, main, open_archive_iterator

ARC_NAME = "TEST-EOT-2004-20041014205819-00000.arc"

# The two lines that follow a filedesc:// header line. Their bytes count toward the filedesc
# record's declared length, which is why LENGTH below is computed rather than written out.
ARC_VERSION_BLOCK = b"1 1 InternetArchive\nURL IP-address Archive-date Content-type Archive-length\n"

FILEDESC_BODY = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<arcmetadata xmlns="http://archive.org/arc/1.0/">\n'
    b"<arc:software>Heritrix 1.0.5-200410121100 http://crawler.archive.org</arc:software>\n"
    b"<dcterms:isPartOf>NARA-GOV-CRAWL-A</dcterms:isPartOf>\n"
    b"<dc:format>ARC file version 1.1</dc:format>\n"
    b"</arcmetadata>\n"
)


def arc_member(header_fields, body):
    """One ARC record as its own gzip member.

    Per-record gzip framing is what makes a byte range from manifest.csv a standalone archive —
    the same property the WARC tests rely on, and the reason the offsets are worth publishing.
    The declared length covers the body only; the trailing newline is the record separator.
    """
    uri, ip, date, content_type = header_fields
    headerline = f"{uri} {ip} {date} {content_type} {len(body)}\n".encode()
    return gzip.compress(headerline + body + b"\n")


def http_response(status_line, headers, body):
    """An HTTP message as an ARC record body: headers and payload, exactly as it came off the wire."""
    head = status_line + "\r\n" + "".join(f"{n}: {v}\r\n" for n, v in headers) + "\r\n"
    return head.encode() + body


# (header fields, body, expected mime, expected extension). The dns: records are the point of the
# fixture: no HTTP layer, so their only declared type is field 4 of the ARC header line.
CAPTURES = [
    (
        ("dns:ntsb.gov", "207.241.224.11", "20041014205819", "text/dns"),
        b"20041014205819\nntsb.gov.\t\t600\tIN\tA\t199.173.155.8\n",
        "text/dns",
        ".txt",
    ),
    (
        ("dns:4women.gov", "207.241.224.11", "20041014205818", "text/dns"),
        b"20041014205818\n4women.gov.\t\t86400\tIN\tA\t12.20.225.1\n",
        "text/dns",
        ".txt",
    ),
    (
        ("http://sba.gov/robots.txt", "199.171.55.3", "20041014205821", "text/plain"),
        http_response(
            "HTTP/1.1 200 OK",
            [("Server", "Netscape-Enterprise/3.6 SP3"), ("Content-type", "text/plain")],
            b"User-agent: *\nDisallow: /cgi-bin/\n",
        ),
        "text/plain",
        ".txt",
    ),
    (
        ("http://energystar.gov/index.html", "208.254.22.7", "20041014205821", "text/html"),
        http_response(
            "HTTP/1.1 404 Not Found",
            [("Server", "Microsoft-IIS/5.0"), ("Content-Type", "text/html; charset=iso-8859-1")],
            b"<html><body>Not Found</body></html>",
        ),
        "text/html",
        ".html",
    ),
    (
        # Heritrix wrote a literal "no-type" when it could not tell. Faithfully reported, not
        # laundered into application/octet-stream.
        ("http://usitc.gov/blob", "192.0.2.7", "20041014205823", "no-type"),
        b"\x00\x01\x02binary-with-no-http-layer",
        "no-type",
        ".unk",
    ),
]


@pytest.fixture
def arc_path(tmp_path):
    path = tmp_path / f"{ARC_NAME}.gz"
    filedesc_body = ARC_VERSION_BLOCK + FILEDESC_BODY
    header = f"filedesc://{ARC_NAME} 0.0.0.0 20041014205819 text/plain {len(filedesc_body)}\n".encode()
    chunks = [gzip.compress(header + filedesc_body + b"\n")]
    chunks += [arc_member(fields, body) for fields, body, _, _ in CAPTURES]
    path.write_bytes(b"".join(chunks))
    return path


def read_csv(zf, suffix):
    name = next(n for n in zf.namelist() if n.endswith(suffix))
    return list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))


def manifest(zf):
    rows = read_csv(zf, "manifest.csv")
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def convert(arc_path, tmp_path, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.zip"
    assert main(str(arc_path), str(out), **kwargs) == 0
    return zipfile.ZipFile(out)


def test_the_fixture_is_a_readable_arc(arc_path):
    """Guards the fixture itself: a malformed ARC would make every assertion below vacuous."""
    with arc_path.open("rb") as fh:
        types = [rec.rec_type for rec in open_archive_iterator(fh)]
    assert types == ["warcinfo"] + ["response"] * len(CAPTURES)


def test_every_arc_record_reaches_the_manifest(arc_path, tmp_path):
    """The regression: ARC has no WARC-Record-ID, so all records used to collide under key None
    and manifest.csv came out with a single row — the last record — while the payloads were all
    written. One row per record is the whole point of the manifest."""
    rows = manifest(convert(arc_path, tmp_path))
    assert len(rows) == len(CAPTURES)
    assert [r["warc_target_uri"] for r in rows] == [fields[0] for fields, _, _, _ in CAPTURES]


def test_arc_header_fields_are_mapped_not_dropped(arc_path, tmp_path):
    rows = manifest(convert(arc_path, tmp_path))
    assert all(r["warc_target_uri"] for r in rows)
    # ARC's compact 14-digit date, surfaced as ISO 8601 like every WARC row
    assert rows[0]["warc_date"] == "2004-10-14T20:58:19Z"
    assert rows[2]["http_status_code"] == "200"
    assert rows[3]["http_status_code"] == "404"


def test_declared_type_survives_for_records_with_no_http_layer(arc_path, tmp_path):
    """warcio's ARC->WARC mapper overwrites the ARC content-type with application/http;
    msgtype=response. For a dns: record that erases the only mime info in the archive."""
    zf = convert(arc_path, tmp_path)
    rows = manifest(zf)
    assert [r["detected_mime_type"] for r in rows] == [mime for _, _, mime, _ in CAPTURES]

    names = {n.rsplit("/", 1)[-1] for n in zf.namelist()}
    for row, (_, _, _, ext) in zip(rows, CAPTURES):
        assert row["filename"].endswith(ext), row["filename"]
        assert row["filename"] in names

    # ...and it is greppable in its own right, not just implied by the extension
    warc_rows = read_csv(zf, "response_warc_headers.csv")[1:]
    assert ("arc_content_type", "text/dns") in {(r[1], r[2]) for r in warc_rows}


def test_synthesized_record_ids_never_reach_the_output(arc_path, tmp_path):
    """warcio mints a fresh random WARC-Record-ID per ARC record so the grouping dict has a key.
    It is not in the archive and differs on every run, so publishing it would invite a CSV
    consumer to treat a per-run UUID as an identity."""
    zf = convert(arc_path, tmp_path)
    assert {r["warc_record_id"] for r in manifest(zf)} == {""}

    entries = [json.loads(line) for line in zf.read(
        next(n for n in zf.namelist() if n.endswith("manifest.jsonl"))
    ).decode().splitlines()]
    assert {e["warc_record_id"] for e in entries} == {""}

    header_names = {r[1] for r in read_csv(zf, "response_warc_headers.csv")[1:]}
    assert "warc_record_id" not in header_names
    # the faithfully-renamed ARC fields are still there
    assert {"warc_target_uri", "warc_date", "warc_ip_address"} <= header_names

    # The filedesc record gets a minted id too, and that one also lands in a file compared
    # byte-for-byte across runs by test_metadata_only_matches_a_full_arc_run.
    assert "warc_record_id" not in {r[1] for r in read_csv(zf, "warcinfo.csv")[1:]}
    assert b"WARC-Record-ID" not in zf.read(
        next(n for n in zf.namelist() if n.endswith("warcinfo.warc"))
    )


def test_filedesc_record_becomes_the_warcinfo(arc_path, tmp_path):
    """ARC's filedesc:// record is the crawl-level provenance record, so it belongs in
    warcinfo.*, and its name is what the root directory is built from."""
    zf = convert(arc_path, tmp_path)
    names = {n.rsplit("/", 1)[-1] for n in zf.namelist()}
    assert {"warcinfo.warc", "warcinfo.warc-fields"} <= names

    fields = zf.read(next(n for n in zf.namelist() if n.endswith("warcinfo.warc-fields")))
    assert b"Heritrix 1.0.5" in fields
    assert b"ARC file version 1.1" in fields

    root = next(n for n in zf.namelist()).split("/", 1)[0]
    assert root.startswith(extract_crawl_name(ARC_NAME) + "_")
    assert manifest(zf)[0]["warc_filename"] == ARC_NAME


def test_manifest_offsets_locate_the_record_inside_the_arc(arc_path, tmp_path):
    """The CSV-only promise: a filtered manifest row must be re-fetchable on its own, by byte
    range, without the zip it came from."""
    zf = convert(arc_path, tmp_path)
    raw = arc_path.read_bytes()

    for row in manifest(zf):
        chunk = raw[int(row["warc_record_offset"]):][: int(row["warc_record_length"])]
        seen = 0
        # Read the payload inside the loop: advancing the iterator drains the previous record's
        # stream, so collecting records first would hand back exhausted streams.
        for record in open_archive_iterator(io.BytesIO(chunk)):
            assert record.rec_headers.get_header("WARC-Target-URI") == row["warc_target_uri"]
            payload = record.content_stream().read()
            seen += 1
        assert seen == 1
        assert len(payload) == int(row["payload_size"])
        assert payload == zf.read(next(n for n in zf.namelist() if n.endswith("/" + row["filename"])))


def test_sidecar_format_groups_arc_captures_by_host(arc_path, tmp_path):
    zf = convert(arc_path, tmp_path, output_format="sidecar")
    dirs = {n.split("/")[1] for n in zf.namelist() if n.count("/") > 1}
    assert {"sba.gov", "energystar.gov", "usitc.gov"} <= dirs


@pytest.mark.parametrize("output_format", ["flat", "sidecar"])
def test_metadata_only_matches_a_full_arc_run(arc_path, tmp_path, output_format):
    """Same invariant the WARC suite protects: --metadata-only changes what is written, never
    what the metadata says."""
    full = convert(arc_path, tmp_path / "full", output_format=output_format)
    meta = convert(arc_path, tmp_path / "meta", output_format=output_format, metadata_only=True)

    payloads = {row["filename"] for row in manifest(full)}
    full_meta = {n.rsplit("/", 1)[-1]: full.read(n) for n in full.namelist()}
    meta_meta = {n.rsplit("/", 1)[-1]: meta.read(n) for n in meta.namelist()}

    assert payloads <= set(full_meta)
    assert payloads.isdisjoint(meta_meta)
    for name, content in meta_meta.items():
        assert full_meta[name] == content, name
