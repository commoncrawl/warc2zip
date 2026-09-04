"""--fetch: rebuilding a subset .warc.gz from manifest rows.

Everything runs offline: the sources are local paths, which fsspec serves through the same
cat_file(start, end) range read that https:// and s3:// use.
"""

import csv
import io
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import CAPTURES
from fsspec.implementations.local import LocalFileSystem
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from warc2zip import (
    FETCH_MAX_BACKOFF,
    FetchLengthMismatch,
    FetchRow,
    RateLimiter,
    cli,
    coalesce_ranges,
    default_fetch_output_path,
    fetch_main,
    fetch_with_retry,
    leading_warcinfo,
    main,
    validate_record_slice,
)


def zip_csv(zip_path, suffix):
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(suffix))
        return list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))


def manifest_rows(zip_path):
    header, *rows = zip_csv(zip_path, "manifest.csv")
    return [dict(zip(header, row)) for row in rows]


def warcinfo_range(zip_path):
    values = {name: value for key, name, value in zip_csv(zip_path, "warcinfo.csv")[1:] if key == "warcinfo"}
    return int(values["warc_record_offset"]), int(values["warc_record_length"])


def request_range(zip_path, filename):
    """(offset, length) of the request record joined to a payload file."""
    values = {name: value for key, name, value in zip_csv(zip_path, "request_warc_headers.csv")[1:] if key == filename}
    return int(values["warc_record_offset"]), int(values["warc_record_length"])


def slice_of(raw, row):
    offset, length = int(row["warc_record_offset"]), int(row["warc_record_length"])
    return raw[offset : offset + length]


def fetch_row(row, **overrides):
    values = {
        "source_uri": row["source_uri"],
        "offset": int(row["warc_record_offset"]),
        "length": int(row["warc_record_length"]),
        "record_id": row["warc_record_id"],
        "target_uri": row["warc_target_uri"],
        "line": 2,
    }
    values.update(overrides)
    return FetchRow(**values)


def write_subset(path, rows, fieldnames=None):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or list(rows[0].keys()), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return path


def record_types_and_ids(path):
    with open(path, "rb") as fh:
        return [(r.rec_type, r.rec_headers.get_header("WARC-Record-ID")) for r in ArchiveIterator(fh)]


def write_warc_without_warcinfo(path):
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
    return path


@pytest.fixture
def metadata_zip(warc_path, tmp_path):
    out = tmp_path / "meta.zip"
    assert main(str(warc_path), str(out), metadata_only=True) == 0
    return out


# --- end to end ---------------------------------------------------------------------------


def test_fetch_rebuilds_the_subset_byte_for_byte(warc_path, metadata_zip, tmp_path):
    """The output is the source's warcinfo member followed by each kept row's member, verbatim."""
    rows = manifest_rows(metadata_zip)
    kept = [rows[0], rows[2]]
    subset = write_subset(tmp_path / "subset.csv", kept)
    out = tmp_path / "subset.warc.gz"

    assert fetch_main(str(subset), str(out)) == 0

    raw = warc_path.read_bytes()
    offset, length = warcinfo_range(metadata_zip)
    assert out.read_bytes() == raw[offset : offset + length] + b"".join(slice_of(raw, row) for row in kept)
    assert record_types_and_ids(out) == [("warcinfo", record_types_and_ids(warc_path)[0][1])] + [
        ("response", row["warc_record_id"]) for row in kept
    ]


def test_fetched_subset_reconverts_and_still_names_the_original_warc(metadata_zip, tmp_path):
    """The README caveat: a derived WARC keeps the original's warcinfo, while its offsets index itself."""
    rows = manifest_rows(metadata_zip)
    subset = write_subset(tmp_path / "subset.csv", [rows[1]])
    out = tmp_path / "subset.warc.gz"
    assert fetch_main(str(subset), str(out)) == 0

    check = tmp_path / "check.zip"
    assert main(str(out), str(check)) == 0
    check_rows = manifest_rows(check)

    assert [row["warc_record_id"] for row in check_rows] == [rows[1]["warc_record_id"]]
    assert check_rows[0]["warc_filename"] == "test.warc.gz"
    assert check_rows[0]["source_uri"] == str(out)
    record = next(iter(ArchiveIterator(io.BytesIO(slice_of(out.read_bytes(), check_rows[0])))))
    assert record.rec_headers.get_header("WARC-Record-ID") == rows[1]["warc_record_id"]


def test_rows_are_grouped_by_source_and_ordered_by_offset(warc_path, metadata_zip, tmp_path):
    """Two sources: each gets its own warcinfo, in first-appearance order, rows sorted within."""
    other = tmp_path / "other.warc.gz"
    shutil.copy(warc_path, other)
    rows = manifest_rows(metadata_zip)
    from_other = [dict(row, source_uri=str(other)) for row in rows]
    # CSV order deliberately scrambled: other first, then this file's rows descending.
    subset = write_subset(tmp_path / "subset.csv", [from_other[1], rows[2], rows[0], from_other[0]])
    out = tmp_path / "subset.warc.gz"

    assert fetch_main(str(subset), str(out)) == 0

    warcinfo_id = record_types_and_ids(warc_path)[0][1]
    assert record_types_and_ids(out) == [
        ("warcinfo", warcinfo_id),
        ("response", rows[0]["warc_record_id"]),
        ("response", rows[1]["warc_record_id"]),
        ("warcinfo", warcinfo_id),
        ("response", rows[0]["warc_record_id"]),
        ("response", rows[2]["warc_record_id"]),
    ]


def test_source_without_warcinfo_yields_responses_only(tmp_path, capsys):
    bare = write_warc_without_warcinfo(tmp_path / "bare.warc.gz")
    meta = tmp_path / "meta.zip"
    assert main(str(bare), str(meta), metadata_only=True) == 0
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(meta))
    out = tmp_path / "subset.warc.gz"

    assert fetch_main(str(subset), str(out)) == 0

    assert [t for t, _ in record_types_and_ids(out)] == ["response"]
    assert "no leading warcinfo record" in capsys.readouterr().err


def test_blank_offset_row_is_skipped_with_a_warning(metadata_zip, tmp_path, capsys):
    rows = manifest_rows(metadata_zip)
    rows[1]["warc_record_offset"] = ""
    subset = write_subset(tmp_path / "subset.csv", rows)
    out = tmp_path / "subset.warc.gz"

    assert fetch_main(str(subset), str(out)) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "1 row(s) could not be fetched" in err
    assert [t for t, _ in record_types_and_ids(out)] == ["warcinfo", "response", "response"]


def test_duplicate_rows_are_fetched_once(metadata_zip, tmp_path, capsys):
    rows = manifest_rows(metadata_zip)
    subset = write_subset(tmp_path / "subset.csv", [rows[0], rows[0]])
    out = tmp_path / "subset.warc.gz"

    assert fetch_main(str(subset), str(out)) == 0

    assert [t for t, _ in record_types_and_ids(out)] == ["warcinfo", "response"]
    assert "1 duplicate row(s) dropped" in capsys.readouterr().err


def test_a_server_that_ignores_range_is_caught_not_retried(metadata_zip, tmp_path, monkeypatch, capsys):
    """fsspec never checks for a 206, so the length check is the only guard against a whole-file answer."""
    monkeypatch.setattr(LocalFileSystem, "cat_file", lambda self, path, start=None, end=None, **kw: Path(path).read_bytes())
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(metadata_zip)[:1])
    out = tmp_path / "subset.warc.gz"
    sleeps = []

    assert fetch_main(str(subset), str(out), sleep=sleeps.append) == 1

    assert sleeps == []
    assert "got" in capsys.readouterr().err
    assert [t for t, _ in record_types_and_ids(out)] == ["warcinfo"]


def test_csv_without_manifest_columns_is_a_usage_error(tmp_path, capsys):
    subset = write_subset(tmp_path / "subset.csv", [{"url": "x", "warc_record_offset": "0"}])
    with pytest.raises(SystemExit) as exc:
        fetch_main(str(subset), str(tmp_path / "out.warc.gz"))
    assert exc.value.code == 2
    assert "source_uri" in capsys.readouterr().err


# --- pure pieces --------------------------------------------------------------------------


def row_at(offset, length):
    return FetchRow("src", offset, length, "", "", 0)


def test_coalesce_ranges_merges_near_rows_and_splits_on_gap_or_span():
    a, b, c = row_at(0, 100), row_at(150, 100), row_at(10_000, 50)
    assert coalesce_ranges([a, b, c], max_gap=100, max_span=10**6) == [(0, 250, [a, b]), (10_000, 10_050, [c])]
    assert coalesce_ranges([a, b], max_gap=10, max_span=10**6) == [(0, 100, [a]), (150, 250, [b])]
    assert coalesce_ranges([a, b], max_gap=100, max_span=200) == [(0, 100, [a]), (150, 250, [b])]
    assert coalesce_ranges([a]) == [(0, 100, [a])]
    assert coalesce_ranges([]) == []


class FakeHTTPError(Exception):
    """Shaped like aiohttp.ClientResponseError: a .status and .headers."""

    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers or {}


def failing(exc):
    def fetch():
        raise exc

    return fetch


def test_fetch_with_retry_honours_retry_after_then_succeeds(capsys):
    outcomes = [FakeHTTPError(503, {"Retry-After": "3"}), FakeHTTPError(429, {"Retry-After": "3"}), b"ok"]

    def fetch():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []
    assert fetch_with_retry(fetch, retries=3, label="x", sleep=sleeps.append) == b"ok"
    assert sleeps == [3.0, 3.0]
    assert capsys.readouterr().err.count("retrying in 3.0 s") == 2


def test_fetch_with_retry_backs_off_exponentially_then_gives_up():
    sleeps = []
    with pytest.raises(FakeHTTPError):
        fetch_with_retry(failing(FakeHTTPError(503)), retries=3, sleep=sleeps.append, rng=lambda: 0.5)
    assert sleeps == [2.0, 4.0, 8.0]  # 2**attempt * (0.5 + rng)


def test_fetch_with_retry_caps_the_backoff():
    sleeps = []
    with pytest.raises(FakeHTTPError):
        fetch_with_retry(failing(FakeHTTPError(503)), retries=8, sleep=sleeps.append, rng=lambda: 0.5)
    assert max(sleeps) == FETCH_MAX_BACKOFF


@pytest.mark.parametrize(
    "exc",
    [FileNotFoundError("404"), PermissionError("403"), FakeHTTPError(416), FakeHTTPError(400), FetchLengthMismatch("short")],
)
def test_deterministic_errors_are_not_retried(exc):
    calls = []

    def fetch():
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        fetch_with_retry(fetch, retries=5, sleep=lambda s: pytest.fail("slept"))
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [ConnectionResetError("reset"), TimeoutError("timeout"), OSError("eio")])
def test_connection_errors_are_retried(exc):
    sleeps = []
    with pytest.raises(type(exc)):
        fetch_with_retry(failing(exc), retries=1, sleep=sleeps.append)
    assert len(sleeps) == 1


def test_rate_limiter_spaces_requests_per_key():
    clock = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(2.0, clock=lambda: clock[0], sleep=sleep)
    limiter.wait("a")
    limiter.wait("b")
    assert sleeps == []
    limiter.wait("a")
    assert sleeps == [0.5]
    clock[0] += 10
    limiter.wait("a")
    assert sleeps == [0.5]


def test_rate_limiter_zero_disables():
    limiter = RateLimiter(0, clock=lambda: 0.0, sleep=lambda s: pytest.fail("slept"))
    limiter.wait("a")
    limiter.wait("a")


def test_leading_warcinfo_cuts_the_first_member_only_when_it_is_complete(warc_path, metadata_zip, tmp_path):
    raw = warc_path.read_bytes()
    offset, length = warcinfo_range(metadata_zip)
    assert offset == 0
    assert leading_warcinfo(raw[: 64 * 1024]) == raw[:length]
    assert leading_warcinfo(raw[: length + 10]) == raw[:length]  # probe ends inside the second member
    assert leading_warcinfo(raw[: length - 5]) is None  # probe ends inside the warcinfo itself
    assert leading_warcinfo(b"not a warc") is None
    assert leading_warcinfo(b"") is None
    bare = write_warc_without_warcinfo(tmp_path / "bare.warc.gz")
    assert leading_warcinfo(bare.read_bytes()) is None


def test_validate_record_slice_rejects_impostors(warc_path, metadata_zip):
    rows = manifest_rows(metadata_zip)
    raw = warc_path.read_bytes()
    row = fetch_row(rows[0])
    good = slice_of(raw, rows[0])

    assert validate_record_slice(good, row) is None
    assert validate_record_slice(b"not a warc at all", row)  # warcio sniffs this as an ARC response
    assert "WARC-Record-ID" in validate_record_slice(slice_of(raw, rows[1]), row)
    assert validate_record_slice(good + b"trailing garbage bytes", row)
    assert validate_record_slice(good[:-1], row)
    offset, length = request_range(metadata_zip, rows[0]["filename"])
    assert "request" in validate_record_slice(raw[offset : offset + length], row)

    # ARC rows carry no record id, so the target URI is what identifies the record.
    assert validate_record_slice(good, fetch_row(rows[0], record_id="")) is None
    assert "WARC-Target-URI" in validate_record_slice(good, fetch_row(rows[0], record_id="", target_uri="https://x/"))


def test_default_fetch_output_path():
    assert default_fetch_output_path("subset.csv", run_id="abcd") == Path("subset_abcd.warc.gz")
    assert default_fetch_output_path("/tmp/dir/Manifest.CSV", run_id="abcd") == Path("Manifest_abcd.warc.gz")
    assert default_fetch_output_path("-", run_id="abcd") == Path("stdin_abcd.warc.gz")
    assert default_fetch_output_path("subset.csv").parent == Path(".")


# --- cli ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["subset.csv", "--fetch", "--limit", "1"],
        ["subset.csv", "--fetch", "--format", "sidecar"],
        ["x.warc.gz", "--rate", "1"],
        ["x.warc.gz", "--retries", "2"],
        ["subset.csv", "--fetch", "--dry-run"],
    ],
)
def test_cli_refuses_flags_that_would_otherwise_be_ignored(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["warc2zip", *argv])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 2


def test_cli_fetch_and_default_format_still_flat(warc_path, metadata_zip, tmp_path, monkeypatch):
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(metadata_zip)[:1])
    out = tmp_path / "subset.warc.gz"
    monkeypatch.setattr(sys, "argv", ["warc2zip", str(subset), "--fetch", "--output", str(out), "--rate", "0"])
    assert cli() == 0
    assert [t for t, _ in record_types_and_ids(out)] == ["warcinfo", "response"]

    check = tmp_path / "check.zip"
    monkeypatch.setattr(sys, "argv", ["warc2zip", str(out), "--output", str(check)])
    assert cli() == 0
    with zipfile.ZipFile(check) as zf:
        names = {n.rsplit("/", 1)[-1] for n in zf.namelist()}
    assert "manifest.csv" in names and not any(".request." in n for n in names)  # flat, not sidecar
    assert len(CAPTURES) == 3  # the fixture shape the row indexes above rely on
