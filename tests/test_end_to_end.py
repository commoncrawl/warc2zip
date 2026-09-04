"""End-to-end conversion over a synthetic WARC built in-process (no network).

Covers both output formats and asserts the shipped CSVs parse with a stock csv.reader — the
property that a single NUL byte from the wire used to break for a whole crawl.
"""

import csv
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from conftest import CAPTURES
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from warc2zip import MANIFEST_COLUMNS, default_output_path, format_record_type_summary, main

# Payload files are named {counter}{ext}. Sidecars share the payload's full name and add a second
# suffix (1000000.html.request.json), so a plain endswith(".json") would confuse the two.
PAYLOAD_RE = re.compile(r"^\d+\.[A-Za-z0-9]+$")

EXPECTED_CSVS = {
    "manifest.csv",
    "metadata.csv",
    "metadata_multi.csv",
    "request_http_headers.csv",
    "request_http_headers_multi.csv",
    "request_warc_headers.csv",
    "request_warc_headers_multi.csv",
    "response_http_headers.csv",
    "response_http_headers_multi.csv",
    "response_warc_headers.csv",
    "response_warc_headers_multi.csv",
    "warcinfo.csv",
    "warcinfo_multi.csv",
}

def csv_members(zf):
    return [n for n in zf.namelist() if n.endswith(".csv")]


def basenames(zf):
    return {n.rsplit("/", 1)[-1] for n in zf.namelist()}


def read_csv(zf, suffix):
    """Parse a CSV member with the stock reader — needing no special dialect is the invariant."""
    name = next(n for n in zf.namelist() if n.endswith(suffix))
    return list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))


def read_rows(zf, suffix):
    """Same as read_csv(), without the header row."""
    return read_csv(zf, suffix)[1:]


@pytest.mark.parametrize("output_format", ["flat", "sidecar"])
def test_conversion_produces_parseable_csvs(warc_path, tmp_path, output_format):
    out = tmp_path / f"{output_format}.zip"

    skipped = main(str(warc_path), str(out), output_format=output_format)

    assert skipped == 0
    with zipfile.ZipFile(out) as zf:
        members = csv_members(zf)
        assert {n.rsplit("/", 1)[-1] for n in members} == EXPECTED_CSVS

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


RUN_ID_RE = r"\d{8}T\d{6}_[0-9a-f]{4}"


@pytest.mark.parametrize("limit", [None, 2])
def test_default_output_name_follows_the_root_dirs_rule(warc_path, tmp_path, monkeypatch, limit):
    """No --output: the zip is {basename}_{hex}[_partial].zip, with _partial iff --limit was given.

    That is the root directory's rule, so the zip name tells you whether it holds a sample, and
    the hex is the *same* one the root directory carries, so a zip on disk can be matched to the
    directory it extracts to.
    """
    monkeypatch.chdir(tmp_path)

    main(str(warc_path), output_path=None, limit=limit)

    suffix = "_partial" if limit else ""
    zips = [p.name for p in tmp_path.glob("*.zip")]
    assert len(zips) == 1
    zip_match = re.fullmatch(rf"test_([0-9a-f]{{4}}){suffix}\.zip", zips[0])
    assert zip_match, zips[0]

    with zipfile.ZipFile(tmp_path / zips[0]) as zf:
        root_dirs = {n.split("/", 1)[0] for n in zf.namelist()}
    assert len(root_dirs) == 1
    dir_match = re.fullmatch(rf"test_\d{{8}}T\d{{6}}_([0-9a-f]{{4}}){suffix}", next(iter(root_dirs)))
    assert dir_match, root_dirs
    assert dir_match.group(1) == zip_match.group(1)


def test_default_output_names_do_not_collide_for_same_basename(warc_path, tmp_path, monkeypatch):
    """CC's warc/, crawldiagnostics/ and robotstxt/ files share a basename: two runs, two zips."""
    monkeypatch.chdir(tmp_path)

    main(str(warc_path), output_path=None)
    main(str(warc_path), output_path=None)

    assert len(list(tmp_path.glob("test_*.zip"))) == 2


@pytest.mark.parametrize(
    ("input_file", "expected"),
    [
        # Every input shape the README shows
        ("archive.warc.gz", "archive.zip"),
        ("/data/crawls/archive.warc.gz", "archive.zip"),
        ("s3://commoncrawl/crawl-data/CC-MAIN-2026-34/segments/x/warc/CC-MAIN-0000.warc.gz", "CC-MAIN-0000.zip"),
        ("https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-34/warc/CC-MAIN-0000.warc.gz", "CC-MAIN-0000.zip"),
        (
            (
                "https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/"
                "CC-MAIN-2026-30-500_records.warc.gz?download=true"
            ),
            "CC-MAIN-2026-30-500_records.zip",
        ),
        ("https://eotarchive.s3.amazonaws.com/crawl-data/EOT-2004/NARA-PEOT-2004.arc.gz", "NARA-PEOT-2004.zip"),
        ("plain.warc", "plain.zip"),
        ("-", "stdin.zip"),
    ],
)
def test_default_output_path_handles_every_readme_input_shape(input_file, expected):
    label = expected[: -len(".zip")]
    assert default_output_path(input_file, run_id="abcd").name == f"{label}_abcd.zip"
    assert default_output_path(input_file, partial=True, run_id="abcd").name == f"{label}_abcd_partial.zip"
    # Without a run id one is minted, and it is 4 hex chars like the root directory's
    assert re.fullmatch(rf"{re.escape(label)}_[0-9a-f]{{4}}\.zip", default_output_path(input_file).name)
    # Always the current directory, never the input's
    assert default_output_path(input_file).parent == Path(".")


def test_explicit_output_path_is_used_verbatim(warc_path, tmp_path):
    out = tmp_path / "chosen-name.zip"
    main(str(warc_path), str(out), limit=1)
    assert out.exists()
    assert list(tmp_path.glob("*.zip")) == [out]


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


def test_warcinfo_reaches_the_zip_raw_and_as_csv(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        assert {"warcinfo.warc", "warcinfo.warc-fields"} <= basenames(zf)
        raw_headers = zf.read(next(n for n in zf.namelist() if n.endswith("warcinfo.warc"))).decode("utf-8")
        raw_body = zf.read(next(n for n in zf.namelist() if n.endswith("warcinfo.warc-fields"))).decode("utf-8")
        rows = read_rows(zf, "warcinfo.csv")

    assert "WARC-Type: warcinfo" in raw_headers
    assert "software: warc2zip-tests" in raw_body
    assert ["warcinfo", "warc_type", "warcinfo"] in rows
    assert ["warcinfo", "_body.software", "warc2zip-tests"] in rows


def test_manifest_csv_mirrors_the_jsonl(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        name = next(n for n in zf.namelist() if n.endswith("manifest.jsonl"))
        entries = [json.loads(line) for line in zf.read(name).decode("utf-8").splitlines()]
        header, *rows = read_csv(zf, "manifest.csv")

    assert header == list(MANIFEST_COLUMNS)
    assert len(rows) == len(entries)
    for row, entry in zip(rows, entries):
        assert row == [str(entry[column]) for column in MANIFEST_COLUMNS]


def test_status_code_leads_each_files_response_http_headers(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        rows = read_rows(zf, "response_http_headers.csv")

    first_seen = {}
    for filename, header_name, value in rows:
        first_seen.setdefault(filename, (header_name, value))

    assert len(first_seen) == len(CAPTURES)
    assert set(first_seen.values()) == {("status_code", "200")}


def test_metadata_body_is_shallow_flattened(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        rows = read_rows(zf, "metadata.csv")
        multi = zf.read(next(n for n in zf.namelist() if n.endswith("metadata_multi.csv"))).decode("utf-8")

    fields = {name: value for _, name, value in rows if name.startswith("_body")}

    assert fields["_body.fetchtimems"] == "42"
    assert fields["_body.charset_detected"] == "utf-8\\x00"  # NUL escaped, not dropped
    assert fields["_body.http_header_user_agent"].endswith("continued-on-the-next-line")
    # one level only: the nested CLD2 language list stays a single opaque value
    assert json.loads(fields["_body.languages_cld2"])["languages"][0]["code"] == "zh"
    # the raw blob is gone from the denormalized CSV...
    assert "_body" not in fields
    # ...but the multiline CSV still carries it verbatim, so nothing is lost in flat format
    assert "_body: fetchTimeMs: 42" in multi


def test_request_http_headers_reach_a_csv(warc_path, tmp_path):
    """They used to exist only in sidecar files, so flat format lost them entirely."""
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        pairs = {(name, value) for _, name, value in read_rows(zf, "request_http_headers.csv")}

    assert ("request_method", "GET") in pairs
    assert ("request_target", "/") in pairs
    assert ("host", "example.com") in pairs


@pytest.mark.parametrize("output_format", ["flat", "sidecar"])
def test_metadata_only_drops_payloads_and_changes_nothing_else(warc_path, tmp_path, output_format):
    full = tmp_path / "full.zip"
    meta = tmp_path / "meta.zip"
    main(str(warc_path), str(full), output_format=output_format)
    main(str(warc_path), str(meta), output_format=output_format, metadata_only=True)

    def members(path):
        # strip the randomized root directory so the two runs are comparable
        with zipfile.ZipFile(path) as zf:
            return {n.split("/", 1)[1]: zf.read(n) for n in zf.namelist()}

    full_members, meta_members = members(full), members(meta)

    payloads = {n for n in full_members if PAYLOAD_RE.match(n.rsplit("/", 1)[-1])}
    assert len(payloads) == len(CAPTURES)
    assert not payloads & set(meta_members)

    metadata_members = set(full_members) - payloads
    assert metadata_members == set(meta_members)
    for name in metadata_members:
        assert full_members[name] == meta_members[name], name


def test_manifest_offsets_locate_the_record_inside_the_warc(warc_path, tmp_path):
    """The whole point of offset+length: reading those bytes must yield that exact record.

    Each record is its own gzip member, so the slice is a standalone WARC — which is what makes
    an HTTP range request against the original WARC work.
    """
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        name = next(n for n in zf.namelist() if n.endswith("manifest.jsonl"))
        entries = [json.loads(line) for line in zf.read(name).decode("utf-8").splitlines()]

    raw = warc_path.read_bytes()
    assert len(entries) == len(CAPTURES)
    for entry in entries:
        offset, length = entry["warc_record_offset"], entry["warc_record_length"]
        assert isinstance(offset, int) and isinstance(length, int) and length > 0

        record = next(iter(ArchiveIterator(io.BytesIO(raw[offset : offset + length]))))

        assert record.rec_type == "response"
        assert record.rec_headers.get_header("WARC-Record-ID") == entry["warc_record_id"]
        assert record.rec_headers.get_header("WARC-Target-URI") == entry["warc_target_uri"]


def test_manifest_names_the_source_warc(warc_path, tmp_path):
    """A CSV-only consumer needs the filename on every row to re-fetch without the zip's context."""
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        rows = read_rows(zf, "manifest.csv")
        warcinfo = read_rows(zf, "warcinfo.csv")

    assert {row[MANIFEST_COLUMNS.index("warc_filename")] for row in rows} == {"test.warc.gz"}
    # every row also carries where the file was actually read from, so no join is needed
    assert {row[MANIFEST_COLUMNS.index("source_uri")] for row in rows} == {str(warc_path)}
    # ...and warcinfo.csv records it once at crawl level
    assert ["warcinfo", "source_uri", str(warc_path)] in warcinfo


def test_every_record_type_carries_its_location(warc_path, tmp_path):
    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        for suffix in ("response_warc_headers.csv", "request_warc_headers.csv", "metadata.csv", "warcinfo.csv"):
            names = {name for _, name, _ in read_rows(zf, suffix)}
            assert {"warc_record_offset", "warc_record_length"} <= names, suffix


def test_source_uri_is_recorded_even_without_a_warcinfo_record(tmp_path):
    """A WARC with no warcinfo still has to say where it came from."""
    path = tmp_path / "no-warcinfo.warc.gz"
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        payload = b"<html>bare</html>"
        writer.write_record(
            writer.create_warc_record(
                "https://bare.example.com/",
                "response",
                payload=io.BytesIO(payload),
                length=len(payload),
                http_headers=StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"),
            )
        )

    out = tmp_path / "flat.zip"
    main(str(path), str(out), output_format="flat")

    with zipfile.ZipFile(out) as zf:
        warcinfo = read_rows(zf, "warcinfo.csv")
        rows = read_rows(zf, "manifest.csv")

    assert ["warcinfo", "source_uri", str(path)] in warcinfo
    # WARC-Filename is unavailable, so the input's own basename stands in
    assert {row[MANIFEST_COLUMNS.index("warc_filename")] for row in rows} == {"no-warcinfo.warc.gz"}


def test_record_type_summary_is_aligned_and_flags_unextracted_types():
    from collections import Counter

    counts = Counter(
        {"response": 3639, "request": 3789, "metadata": 3789, "revisit": 150, "warcinfo": 1, "resource": 12, "conversion": 0}
    )
    assert format_record_type_summary(counts) == (
        "Record types:\n"
        "  warcinfo     1\n"
        "  response  3639\n"
        "  revisit    150\n"
        "  request   3789\n"
        "  metadata  3789\n"
        "  resource    12  (not extracted)"
    )
    assert format_record_type_summary(Counter()) == "Record types: none"


def append_revisit_capture(warc_path):
    """Append a CC-style revisit capture (request + 304 revisit + metadata) to the fixture.

    As CC writes it: no WARC-Concurrent-To anywhere; the revisit names its request in
    WARC-Refers-To. Every record is its own gzip member, so appending keeps the WARC valid.
    Returns the capture's target URI.
    """
    uri = "http://revisited.example.org/index.htm"
    with open(warc_path, "ab") as fh:
        writer = WARCWriter(fh, gzip=True)
        request = writer.create_warc_record(
            uri,
            "request",
            http_headers=StatusAndHeaders(
                "GET /index.htm HTTP/1.1",
                [("Host", "revisited.example.org"), ("If-Modified-Since", "Mon, 11 May 2026 12:10:15 GMT")],
                is_http_request=True,
            ),
        )
        writer.write_record(request)
        revisit = writer.create_warc_record(
            uri,
            "revisit",
            http_headers=StatusAndHeaders("304 Not Modified", [("ETag", '"3820"'), ("Content-Length", "0")], protocol="HTTP/1.1"),
            warc_headers_dict={
                "WARC-Profile": "http://netpreserve.org/warc/1.1/revisit/server-not-modified",
                "WARC-Refers-To": request.rec_headers.get_header("WARC-Record-ID"),
                "WARC-Refers-To-Target-URI": uri,
                "WARC-Refers-To-Date": "2026-05-11T12:10:15Z",
            },
        )
        writer.write_record(revisit)
        body = b"fetchTimeMs: 118\r\n"
        writer.write_record(
            writer.create_warc_record(
                uri,
                "metadata",
                payload=io.BytesIO(body),
                length=len(body),
                warc_headers_dict={
                    "WARC-Concurrent-To": revisit.rec_headers.get_header("WARC-Record-ID"),
                    "Content-Type": "application/warc-fields",
                },
            )
        )
    return uri


def test_revisit_capture_is_extracted_without_payload(warc_path, tmp_path, capsys):
    """A revisit capture gets every CSV row a response does — its request joined through the
    revisit's WARC-Refers-To — but no payload file: a 304 has no body of its own."""
    uri = append_revisit_capture(warc_path)

    out = tmp_path / "flat.zip"
    main(str(warc_path), str(out), output_format="flat")
    captured = capsys.readouterr()

    assert ": 3 responses, 1 revisits, 4 requests, 4 metadata records" in captured.out
    table = [line.split() for line in captured.out.split("Record types:\n", 1)[1].splitlines()]
    assert [row[0] for row in table] == ["warcinfo", "response", "revisit", "request", "metadata"]
    assert not any(row[-1] == "extracted)" for row in table)
    assert "Warning" not in captured.err

    with zipfile.ZipFile(out) as zf:
        # The synthetic .revisit key names no member of the zip
        assert not any(n.endswith(".revisit") for n in zf.namelist())
        payloads = [n for n in zf.namelist() if PAYLOAD_RE.match(n.rsplit("/", 1)[-1])]
        assert len(payloads) == 3

        manifest = [dict(zip(MANIFEST_COLUMNS, row)) for row in read_rows(zf, "manifest.csv")]
        assert [row["warc_type"] for row in manifest] == ["response"] * 3 + ["revisit"]
        revisit_row = manifest[-1]
        assert revisit_row["filename"] == "1000003.revisit"
        assert revisit_row["http_status_code"] == "304"
        assert revisit_row["payload_size"] == "0"
        assert revisit_row["warc_refers_to_target_uri"] == uri
        assert revisit_row["warc_refers_to_date"] == "2026-05-11T12:10:15Z"

        request_rows = [r for r in read_rows(zf, "request_http_headers.csv") if r[0] == "1000003.revisit"]
        assert ["1000003.revisit", "if_modified_since", "Mon, 11 May 2026 12:10:15 GMT"] in request_rows
        response_rows = [r for r in read_rows(zf, "response_http_headers.csv") if r[0] == "1000003.revisit"]
        assert response_rows[0][1:] == ["status_code", "304"]
        metadata_rows = [r for r in read_rows(zf, "metadata.csv") if r[0] == "1000003.revisit"]
        assert any("fetchtimems" in name for _, name, _ in metadata_rows)

    # The --metadata-only invariant extends to revisit captures: metadata is byte-identical
    meta_out = tmp_path / "meta.zip"
    main(str(warc_path), str(meta_out), output_format="flat", metadata_only=True)

    def members(path):
        # strip the randomized root directory so the two runs are comparable
        with zipfile.ZipFile(path) as inner:
            return {n.split("/", 1)[1]: inner.read(n) for n in inner.namelist()}

    full_members, meta_members = members(out), members(meta_out)
    metadata_members = {n for n in full_members if not PAYLOAD_RE.match(n.rsplit("/", 1)[-1])}
    assert metadata_members == set(meta_members)
    for name in metadata_members:
        assert full_members[name] == meta_members[name], name


def test_limit_counts_revisit_captures(warc_path, tmp_path):
    append_revisit_capture(warc_path)

    out = tmp_path / "limited.zip"
    main(str(warc_path), str(out), limit=4, output_format="flat")

    with zipfile.ZipFile(out) as zf:
        manifest = [dict(zip(MANIFEST_COLUMNS, row)) for row in read_rows(zf, "manifest.csv")]
    assert len(manifest) == 4
    assert manifest[-1]["warc_type"] == "revisit"
