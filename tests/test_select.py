"""--select: a zip with only the captures a (filtered) manifest.csv names, each one complete."""

import csv
import io
import shutil
import sys
import zipfile

import pytest
from conftest import CAPTURES
from test_arc import arc_path  # noqa: F401 - fixture

from warc2zip import SelectRow, cli, main, row_names_input


def zip_csv(zip_path, suffix):
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith("/" + suffix))
        return list(csv.reader(io.StringIO(zf.read(name).decode("utf-8"))))


def manifest_rows(zip_path):
    header, *rows = zip_csv(zip_path, "manifest.csv")
    return [dict(zip(header, row)) for row in rows]


def without_filename(row):
    return {k: v for k, v in row.items() if k != "filename"}


def keyed_by_record_id(zip_path, name):
    """A header CSV's (header_name, header_value) rows per capture, keyed by the capture's
    WARC-Record-ID rather than its filename, which a selected zip numbers afresh."""
    ids = {r["filename"]: r["warc_record_id"] for r in manifest_rows(zip_path)}
    rows = {}
    for filename, header_name, header_value in zip_csv(zip_path, name)[1:]:
        rows.setdefault(ids[filename], []).append((header_name, header_value))
    return rows


def write_subset(path, rows, fieldnames=None):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or list(rows[0].keys()), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return path


def select(warc, rows, tmp_path, name="selected.zip", **kwargs):
    out = tmp_path / name
    status = main(str(warc), str(out), select=str(write_subset(tmp_path / f"{name}.csv", rows)), **kwargs)
    return status, out


@pytest.fixture
def full_zip(warc_path, tmp_path):
    out = tmp_path / "full.zip"
    assert main(str(warc_path), str(out)) == 0
    return out


# --- end to end ---------------------------------------------------------------------------


def test_only_the_selected_captures_each_one_complete(warc_path, full_zip, tmp_path, capsys):
    """The workflow: --metadata-only, edit the CSV, select from the same WARC. Every CSV holds
    exactly the selected captures' rows from a full run, request and metadata included."""
    rows = manifest_rows(full_zip)
    kept = [rows[0], rows[2]]

    status, out = select(warc_path, kept, tmp_path)

    assert status == 0
    assert [without_filename(r) for r in manifest_rows(out)] == [without_filename(r) for r in kept]
    for name in ("response_warc_headers.csv", "response_http_headers.csv", "request_warc_headers.csv",
                 "request_http_headers.csv", "metadata.csv"):
        full = keyed_by_record_id(full_zip, name)
        assert keyed_by_record_id(out, name) == {r["warc_record_id"]: full[r["warc_record_id"]] for r in kept}, name
    assert zip_csv(out, "warcinfo.csv") == zip_csv(full_zip, "warcinfo.csv")
    err = capsys.readouterr().err
    assert "no response record" not in err  # the dropped captures' records are not orphans


def test_payloads_are_the_selected_ones(warc_path, full_zip, tmp_path):
    rows = manifest_rows(full_zip)
    _, out = select(warc_path, [rows[1]], tmp_path)
    with zipfile.ZipFile(full_zip) as full, zipfile.ZipFile(out) as selected:
        full_payload = full.read(next(n for n in full.namelist() if n.endswith("/" + rows[1]["filename"])))
        payloads = [n for n in selected.namelist() if n.rsplit("/", 1)[-1][:1].isdigit()]
        assert [selected.read(n) for n in payloads] == [full_payload]


def test_the_selected_manifest_selects_again(warc_path, full_zip, tmp_path):
    _, first = select(warc_path, [manifest_rows(full_zip)[1]], tmp_path, "first.zip")
    status, again = select(warc_path, manifest_rows(first), tmp_path, "again.zip")
    assert status == 0
    assert manifest_rows(again) == manifest_rows(first)


def test_select_with_sidecar_format_and_metadata_only(warc_path, full_zip, tmp_path):
    status, out = select(warc_path, manifest_rows(full_zip)[:1], tmp_path, output_format="sidecar", metadata_only=True)
    assert status == 0
    with zipfile.ZipFile(out) as zf:
        basenames = [n.rsplit("/", 1)[-1] for n in zf.namelist()]
        suffixes = {b.split(".", 1)[-1] for b in basenames if b[:1].isdigit()}
    assert {"html.request.warc", "html.metadata.warc-fields", "html.response.http"} <= suffixes
    assert "html" not in suffixes  # metadata-only: no payload


def test_limit_counts_selected_captures(warc_path, full_zip, tmp_path):
    rows = manifest_rows(full_zip)
    status, out = select(warc_path, [rows[1], rows[2]], tmp_path, limit=1)
    assert status == 0
    assert [r["warc_record_id"] for r in manifest_rows(out)] == [rows[1]["warc_record_id"]]


def test_arc_rows_are_selected_by_offset(arc_path, tmp_path):  # noqa: F811
    """ARC rows carry no record id (warcio mints a new one every run), so the offset is the key."""
    full = tmp_path / "full.zip"
    assert main(str(arc_path), str(full)) == 0
    rows = manifest_rows(full)
    assert rows[1]["warc_record_id"] == ""

    status, out = select(arc_path, [rows[1]], tmp_path)

    assert status == 0
    assert [without_filename(r) for r in manifest_rows(out)] == [without_filename(rows[1])]


def test_same_warc_under_another_path_is_selected(warc_path, full_zip, tmp_path):
    """Case 2: the manifest came from one spelling of the file (s3://...), the input is another (a local copy)."""
    moved = tmp_path / "elsewhere" / warc_path.name
    moved.parent.mkdir()
    shutil.copy(warc_path, moved)
    rows = [dict(r, source_uri=f"s3://bucket/crawl/{warc_path.name}") for r in manifest_rows(full_zip)]

    status, out = select(moved, rows, tmp_path)

    assert status == 0
    assert len(manifest_rows(out)) == len(rows)


def test_rows_of_another_warc_are_noted_not_selected(warc_path, full_zip, tmp_path, capsys):
    """Case 3: a subset mixing WARCs is a mistake to report; its foreign offsets are never looked up."""
    rows = manifest_rows(full_zip)
    foreign = [dict(r, source_uri="s3://bucket/other.warc.gz", warc_filename="other.warc.gz") for r in rows[1:]]

    status, out = select(warc_path, [rows[0], *foreign], tmp_path)

    assert status == 0
    assert [r["warc_record_id"] for r in manifest_rows(out)] == [rows[0]["warc_record_id"]]
    assert "2 row(s) name another WARC and were not selected (other.warc.gz: 2)" in capsys.readouterr().err


def test_a_subset_of_only_another_warc_selects_nothing_and_fails(warc_path, full_zip, tmp_path, capsys):
    rows = [dict(r, source_uri="other.warc.gz", warc_filename="other.warc.gz") for r in manifest_rows(full_zip)]
    status, _ = select(warc_path, rows, tmp_path)
    assert status == 1
    assert "selected nothing" in capsys.readouterr().err


def test_an_offset_that_is_no_capture_is_reported(warc_path, full_zip, tmp_path, capsys):
    rows = manifest_rows(full_zip)
    status, out = select(warc_path, [rows[0], dict(rows[1], warc_record_offset="7")], tmp_path)
    assert status == 1
    assert len(manifest_rows(out)) == 1
    assert "1 row(s) of" in capsys.readouterr().err


def test_a_record_id_that_disagrees_with_the_offset_is_not_selected(warc_path, full_zip, tmp_path, capsys):
    """Same name, different file: the offset lands on a capture, but not the one the row describes."""
    rows = manifest_rows(full_zip)
    status, out = select(warc_path, [dict(rows[0], warc_record_id="<urn:uuid:other>")], tmp_path)
    assert status == 1
    assert manifest_rows(out) == []
    assert "not <urn:uuid:other>" in capsys.readouterr().err


def test_blank_offset_row_is_skipped_with_a_warning(warc_path, full_zip, tmp_path, capsys):
    rows = manifest_rows(full_zip)
    rows[1]["warc_record_offset"] = ""
    status, out = select(warc_path, rows, tmp_path)
    assert status == 1
    err = capsys.readouterr().err
    assert "line 3" in err and "1 row(s) could not be selected" in err
    assert len(manifest_rows(out)) == 2


def test_duplicate_rows_select_once(warc_path, full_zip, tmp_path):
    rows = manifest_rows(full_zip)
    status, out = select(warc_path, [rows[0], rows[0]], tmp_path)
    assert status == 0
    assert len(manifest_rows(out)) == 1


def test_a_csv_cut_down_to_the_offset_column_still_selects(warc_path, full_zip, tmp_path):
    rows = manifest_rows(full_zip)
    status, out = select(warc_path, [{"warc_record_offset": rows[2]["warc_record_offset"]}], tmp_path)
    assert status == 0
    assert [r["warc_record_id"] for r in manifest_rows(out)] == [rows[2]["warc_record_id"]]


def test_csv_without_offset_column_is_a_usage_error(warc_path, tmp_path, capsys):
    subset = write_subset(tmp_path / "s.csv", [{"url": "x"}])
    with pytest.raises(SystemExit) as exc:
        main(str(warc_path), str(tmp_path / "out.zip"), select=str(subset))
    assert exc.value.code == 2
    assert "warc_record_offset" in capsys.readouterr().err


# --- row_names_input ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_uri, warc_filename, input_file, input_warc_filename, expected",
    [
        # Case 1: the manifest came from this very file.
        ("/data/test.warc.gz", "test.warc.gz", "/data/test.warc.gz", "test.warc.gz", True),
        # Case 2: same file, another spelling.
        ("s3://commoncrawl/crawl-data/x/test.warc.gz", "test.warc.gz", "./test.warc.gz", "test.warc.gz", True),
        ("https://data.commoncrawl.org/x/test.warc.gz", "test.warc.gz", "/tmp/test.warc.gz", "", True),
        # A renamed copy: only its warcinfo still says what it is.
        ("s3://bucket/test.warc.gz", "test.warc.gz", "/tmp/renamed.warc.gz", "test.warc.gz", True),
        # Case 3: another WARC.
        ("s3://bucket/other.warc.gz", "other.warc.gz", "/data/test.warc.gz", "test.warc.gz", False),
        # A CSV cut down to the offset column names no file: nothing to compare.
        ("", "", "/data/test.warc.gz", "test.warc.gz", True),
    ],
)
def test_row_names_input(source_uri, warc_filename, input_file, input_warc_filename, expected):
    row = SelectRow(0, "", source_uri, warc_filename, 2)
    assert row_names_input(row, input_file, input_warc_filename) is expected


# --- cli ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["x.warc.gz", "--select", "s.csv", "--dry-run"],
        ["-", "--select", "-"],
        ["s.csv", "--fetch"],
        ["x.warc.gz", "--rate", "1"],
    ],
)
def test_cli_refuses_flags_that_would_otherwise_be_ignored(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["warc2zip", *argv])
    with pytest.raises(SystemExit) as exc:
        cli()
    assert exc.value.code == 2


def test_cli_select(warc_path, full_zip, tmp_path, monkeypatch):
    subset = write_subset(tmp_path / "s.csv", manifest_rows(full_zip)[:1])
    out = tmp_path / "selected.zip"
    monkeypatch.setattr(sys, "argv", ["warc2zip", str(warc_path), "--select", str(subset), "--output", str(out)])
    assert cli() == 0
    assert [r["warc_target_uri"] for r in manifest_rows(out)] == [CAPTURES[0][0]]
