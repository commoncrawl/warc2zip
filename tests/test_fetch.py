"""--fetch: turning manifest rows back into a zip.

Everything runs offline: the sources are local paths, which fsspec serves through the same
cat_file(start, end) range read that https:// and s3:// use.
"""

import csv
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from conftest import CAPTURES
from fsspec.implementations.local import LocalFileSystem
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

import warc2zip
from warc2zip import (
    FETCH_MAX_BACKOFF,
    FetchLengthMismatch,
    FetchRow,
    RateLimiter,
    cli,
    coalesce_ranges,
    default_output_path,
    fetch_main,
    fetch_with_retry,
    http_range,
    leading_warcinfo,
    main,
    set_host_interval,
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


def payloads(zip_path):
    """Payload bytes of a flat zip, in manifest order."""
    with zipfile.ZipFile(zip_path) as zf:
        members = {name.rsplit("/", 1)[-1]: name for name in zf.namelist()}
        return [zf.read(members[row["filename"]]) for row in manifest_rows(zip_path)]


def without_filename(rows):
    """Manifest rows minus `filename`, the one column a fetched zip numbers afresh."""
    return [{k: v for k, v in row.items() if k != "filename"} for row in rows]


def warcinfo_values(zip_path, name):
    """{warcinfo key: value} of one field across the warcinfo records of a zip."""
    return {key: value for key, field, value in zip_csv(zip_path, "warcinfo.csv")[1:] if field == name}


BODIES = {capture[0]: capture[2] for capture in CAPTURES}


def assert_fetched(zip_path, rows):
    """The zip holds exactly `rows`: same manifest (original coordinates included), same payloads."""
    assert without_filename(manifest_rows(zip_path)) == without_filename(rows)
    assert payloads(zip_path) == [BODIES[row["warc_target_uri"]] for row in rows]


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


def test_fetch_rebuilds_the_subset(warc_path, metadata_zip, tmp_path):
    """Each kept row comes back as a payload, under a manifest row that still addresses the original."""
    rows = manifest_rows(metadata_zip)
    kept = [rows[0], rows[2]]
    subset = write_subset(tmp_path / "subset.csv", kept)
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 0

    assert_fetched(out, kept)
    # The source's own warcinfo record, at its place in the source.
    assert warcinfo_range(out) == warcinfo_range(metadata_zip)
    assert warcinfo_values(out, "source_uri") == {"warcinfo": str(warc_path)}
    assert warcinfo_values(out, "warc_filename") == {"warcinfo": "test.warc.gz"}
    with zipfile.ZipFile(out) as zf:
        roots = {name.split("/")[0] for name in zf.namelist()}
    assert len(roots) == 1 and roots.pop().startswith("subset_")  # named after the manifest, not a source


def test_the_temporary_warc_is_removed(metadata_zip, tmp_path, monkeypatch):
    """The fetched .warc.gz lives in the temp directory for the length of the run, then goes,
    whether the conversion succeeds or blows up."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(metadata_zip))
    seen = []
    real_main = warc2zip.main

    def spying_main(*args, fetched=None, **kwargs):
        seen.append(Path(fetched.path))
        assert seen[0].parent.parent == scratch and seen[0].stat().st_size > 0
        return real_main(*args, fetched=fetched, **kwargs)

    monkeypatch.setattr(warc2zip, "main", spying_main)
    assert fetch_main(str(subset), str(tmp_path / "subset.zip")) == 0
    assert seen and list(scratch.iterdir()) == []

    monkeypatch.setattr(warc2zip, "main", lambda *args, **kwargs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        fetch_main(str(subset), str(tmp_path / "again.zip"))
    assert list(scratch.iterdir()) == []


def test_fetched_manifest_can_be_fetched_again(warc_path, metadata_zip, tmp_path):
    """The zip's own manifest.csv is a valid --fetch input: its rows address the original WARC."""
    rows = manifest_rows(metadata_zip)
    first = tmp_path / "first.zip"
    assert fetch_main(str(write_subset(tmp_path / "subset.csv", rows)), str(first)) == 0

    again = write_subset(tmp_path / "again.csv", manifest_rows(first)[1:])
    second = tmp_path / "second.zip"
    assert fetch_main(str(again), str(second)) == 0

    assert_fetched(second, rows[1:])


def test_rows_are_grouped_by_source_and_ordered_by_offset(warc_path, metadata_zip, tmp_path):
    """Two sources: first-appearance order, rows sorted within, each row labelled with its own source."""
    other = tmp_path / "other.warc.gz"
    shutil.copy(warc_path, other)
    rows = manifest_rows(metadata_zip)
    from_other = [dict(row, source_uri=str(other)) for row in rows]
    # CSV order deliberately scrambled: other first, then this file's rows descending.
    subset = write_subset(tmp_path / "subset.csv", [from_other[1], rows[2], rows[0], from_other[0]])
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 0

    # The copy carries the same record ids, which must not merge the two sources' rows.
    assert_fetched(out, [from_other[0], from_other[1], rows[0], rows[2]])
    assert warcinfo_values(out, "source_uri") == {"warcinfo": str(other), "warcinfo.1": str(warc_path)}
    assert warcinfo_values(out, "warc_record_offset") == {"warcinfo": "0", "warcinfo.1": "0"}


def test_source_without_warcinfo_is_labelled_by_its_own_name(tmp_path, capsys):
    bare = write_warc_without_warcinfo(tmp_path / "bare.warc.gz")
    meta = tmp_path / "meta.zip"
    assert main(str(bare), str(meta), metadata_only=True) == 0
    rows = manifest_rows(meta)
    subset = write_subset(tmp_path / "subset.csv", rows)
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 0

    assert without_filename(manifest_rows(out)) == without_filename(rows)
    assert manifest_rows(out)[0]["warc_filename"] == "bare.warc.gz"
    assert payloads(out) == [b"<html>bare</html>"]
    assert zip_csv(out, "warcinfo.csv")[1:] == []
    assert "no leading warcinfo record" in capsys.readouterr().err


def test_blank_offset_row_is_skipped_with_a_warning(metadata_zip, tmp_path, capsys):
    rows = manifest_rows(metadata_zip)
    subset = write_subset(tmp_path / "subset.csv", [rows[0], dict(rows[1], warc_record_offset=""), rows[2]])
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 1

    err = capsys.readouterr().err
    assert "line 3" in err and "1 row(s) could not be fetched" in err
    assert_fetched(out, [rows[0], rows[2]])


def test_duplicate_rows_are_fetched_once(metadata_zip, tmp_path, capsys):
    rows = manifest_rows(metadata_zip)
    subset = write_subset(tmp_path / "subset.csv", [rows[0], rows[0]])
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 0

    assert_fetched(out, [rows[0]])
    assert "1 duplicate row(s) dropped" in capsys.readouterr().err


def test_a_server_that_ignores_range_is_caught_not_retried(metadata_zip, tmp_path, monkeypatch, capsys):
    """fsspec never checks for a 206, so the length check is the only guard against a whole-file answer."""
    monkeypatch.setattr(LocalFileSystem, "cat_file", lambda self, path, start=None, end=None, **kw: Path(path).read_bytes())
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(metadata_zip)[:1])
    out = tmp_path / "subset.zip"
    sleeps = []

    assert fetch_main(str(subset), str(out), sleep=sleeps.append) == 1

    assert sleeps == []
    assert "got" in capsys.readouterr().err
    # Still a well-formed zip: the warcinfo the probe found, and no captures.
    assert manifest_rows(out) == []
    assert set(warcinfo_values(out, "source_uri")) == {"warcinfo"}


def test_nothing_fetched_still_yields_a_named_zip(tmp_path):
    """Every row failing must not write the members under a root directory called None."""
    row = {"source_uri": str(tmp_path / "missing.warc.gz"), "warc_record_offset": "0", "warc_record_length": "10"}
    out = tmp_path / "subset.zip"

    assert fetch_main(str(write_subset(tmp_path / "subset.csv", [row])), str(out), sleep=lambda s: None) == 1

    with zipfile.ZipFile(out) as zf:
        assert all(name.startswith("subset_") for name in zf.namelist())


def test_csv_without_manifest_columns_is_a_usage_error(tmp_path, capsys):
    subset = write_subset(tmp_path / "subset.csv", [{"url": "x", "warc_record_offset": "0"}])
    with pytest.raises(SystemExit) as exc:
        fetch_main(str(subset), str(tmp_path / "out.zip"))
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


def test_default_zip_is_named_after_the_manifest():
    assert default_output_path("subset.csv", run_id="abcd") == Path("subset_abcd.zip")
    assert default_output_path("/tmp/dir/Manifest.CSV", run_id="abcd") == Path("Manifest_abcd.zip")
    assert default_output_path("-", run_id="abcd") == Path("stdin_abcd.zip")


# --- cli ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["subset.csv", "--fetch", "--limit", "1"],
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


def test_cli_fetch_writes_a_zip_in_either_format(warc_path, metadata_zip, tmp_path, monkeypatch):
    rows = manifest_rows(metadata_zip)[:1]
    subset = write_subset(tmp_path / "subset.csv", rows)
    out = tmp_path / "subset.zip"
    monkeypatch.setattr(sys, "argv", ["warc2zip", str(subset), "--fetch", "--output", str(out), "--rate", "0"])
    assert cli() == 0
    assert_fetched(out, rows)
    with zipfile.ZipFile(out) as zf:
        assert not any(".response." in name for name in zf.namelist())  # flat by default

    sidecar = tmp_path / "sidecar.zip"
    monkeypatch.setattr(
        sys, "argv", ["warc2zip", str(subset), "--fetch", "--format", "sidecar", "--output", str(sidecar)]
    )
    assert cli() == 0
    with zipfile.ZipFile(sidecar) as zf:
        assert any(name.endswith("/example.com/1000000.html.response.http") for name in zf.namelist())
    assert without_filename(manifest_rows(sidecar)) == without_filename(rows)


def test_cli_fetch_default_output_is_a_zip_in_cwd(warc_path, metadata_zip, tmp_path, monkeypatch):
    subset = write_subset(tmp_path / "subset.csv", manifest_rows(metadata_zip)[:1])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["warc2zip", str(subset), "--fetch"])
    assert cli() == 0
    (out,) = tmp_path.glob("subset_*.zip")
    with zipfile.ZipFile(out) as zf:
        root = zf.namelist()[0].split("/")[0]
    assert root.endswith(out.stem.split("_")[-1])  # the zip and its root directory share the run id


# --- http(s) transport through cdx_toolkit ------------------------------------------------


class FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


def fake_myrequests_get(warc_bytes, log, redirect_from=None, ignore_range=False):
    """A `myrequests_get` stand-in serving Range requests for `warc_bytes`, optionally after a 302."""

    def get(url, params=None, headers=None, **kwargs):
        log.append((url, headers["Range"], kwargs))
        if url == redirect_from:
            return FakeResponse(b"<html>moved</html>", 302, {"Location": "/cdn/x.warc.gz"})
        if ignore_range:
            return FakeResponse(warc_bytes, 200)
        start, end = (int(v) for v in headers["Range"][len("bytes=") :].split("-"))
        return FakeResponse(warc_bytes[start : end + 1], 206)

    return get


def https_rows(rows, uri):
    return [dict(row, source_uri=uri) for row in rows]


def test_https_sources_go_through_cdx_toolkit(warc_path, metadata_zip, tmp_path, monkeypatch):
    log = []
    monkeypatch.setattr(warc2zip, "myrequests_get", fake_myrequests_get(warc_path.read_bytes(), log))
    uri = "https://data.example.org/crawl-data/x.warc.gz"
    rows = manifest_rows(metadata_zip)
    subset = write_subset(tmp_path / "subset.csv", https_rows([rows[0], rows[2]], uri))
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out), retries=3) == 0

    assert_fetched(out, https_rows([rows[0], rows[2]], uri))
    assert warcinfo_values(out, "source_uri") == {"warcinfo": uri}
    # One probe plus one coalesced span, both Range requests to cdx_toolkit's transport.
    assert [(u, r) for u, r, _ in log] == [
        (uri, "bytes=0-65535"),
        (uri, f"bytes={rows[0]['warc_record_offset']}-{int(rows[2]['warc_record_offset']) + int(rows[2]['warc_record_length']) - 1}"),
    ]
    assert all(kw == {"raise_error_after_n_errors": 3} for _, _, kw in log)


def test_http_range_follows_redirects_by_hand(warc_path):
    """myrequests_get passes allow_redirects=False, so a 302 would otherwise come back as the slice."""
    log = []
    raw = warc_path.read_bytes()
    get = fake_myrequests_get(raw, log, redirect_from="https://hub.example/resolve/x.warc.gz?download=true")

    data = http_range("https://hub.example/resolve/x.warc.gz?download=true", 10, 20, get=get)

    assert data == raw[10:20]
    assert [u for u, _, _ in log] == ["https://hub.example/resolve/x.warc.gz?download=true", "https://hub.example/cdn/x.warc.gz"]
    assert {r for _, r, _ in log} == {"bytes=10-19"}


def test_http_range_gives_up_on_a_redirect_loop(warc_path):
    log = []
    get = fake_myrequests_get(warc_path.read_bytes(), log, redirect_from="https://hub.example/cdn/x.warc.gz")
    with pytest.raises(RuntimeError, match="redirects"):
        http_range("https://hub.example/cdn/x.warc.gz", 0, 10, redirects=2, get=get)
    assert len(log) == 3


def test_https_server_ignoring_range_is_caught(warc_path, metadata_zip, tmp_path, monkeypatch, capsys):
    log = []
    monkeypatch.setattr(warc2zip, "myrequests_get", fake_myrequests_get(warc_path.read_bytes(), log, ignore_range=True))
    rows = https_rows(manifest_rows(metadata_zip)[:1], "https://data.example.org/x.warc.gz")
    subset = write_subset(tmp_path / "subset.csv", rows)
    out = tmp_path / "subset.zip"

    assert fetch_main(str(subset), str(out)) == 1

    assert "got" in capsys.readouterr().err
    assert manifest_rows(out) == []
    assert set(warcinfo_values(out, "source_uri")) == {"warcinfo"}  # the probe tolerates a long answer


def test_set_host_interval_maps_rate_onto_cdx_toolkit(monkeypatch):
    from cdx_toolkit.myrequests import retry_info

    host = "data.example.org"
    monkeypatch.delitem(retry_info, host, raising=False)
    set_host_interval(host, None)
    assert host not in retry_info  # None leaves cdx_toolkit's own defaults alone
    set_host_interval(host, 4)
    assert retry_info[host]["minimum_interval"] == 0.25
    set_host_interval(host, 0)
    assert retry_info[host]["minimum_interval"] == 0.0
    assert retry_info["data.commoncrawl.org"]["minimum_interval"] == 0.55
