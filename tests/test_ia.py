"""ia://item/file inputs through fsspec's InternetArchiveFileSystem.

The class itself (URI mapping, ia.ini parsing, cookie-jar scoping) is tested in fsspec; what is
tested here is warc2zip's use of it: that the installed fsspec provides the protocol, a conversion
and a --fetch through it against the `ia_server` fixture (a stand-in archive.org whose /download/
URL 302s to a data node on another origin, serving byte ranges, optionally only to a logged-in
cookie), and the CLI's explanation of a refused item. The one network test lives in
test_readme_warcs.py.
"""

import pytest
from conftest import CAPTURES
from fsspec.implementations.ia import InternetArchiveFileSystem, load_ia_credentials
from test_fetch import assert_is_stamped_copy, manifest_rows, parsed_records, slice_of, warcinfo_range, write_subset

import warc2zip
from warc2zip import (
    cli,
    default_output_path,
    fetch_main,
    format_ia_permission_error,
    input_basename,
    main,
)

INI = """[s3]
access = AKIA-TEST
secret = s3cr3t
[cookies]
logged-in-user = someone%40example.org; expires=Sat, 28-Aug-2027 19:39:52 GMT; Max-Age=31536000; path=/; domain=.archive.org
logged-in-sig = 1756000000-abcdef0123456789; expires=Sat, 28-Aug-2027 19:39:52 GMT; Max-Age=31536000; path=/; domain=.archive.org
[general]
screenname = Some One
"""


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    """Never read the developer's real ia.ini, and never reuse a cached filesystem (fsspec caches
    instances by constructor arguments, so a no-argument instance would carry the previous test's
    credentials)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("IA_CONFIG_FILE", "XDG_CONFIG_HOME", "IA_ACCESS_KEY_ID", "IA_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    InternetArchiveFileSystem.clear_instance_cache()
    yield
    InternetArchiveFileSystem.clear_instance_cache()


def write_ini(path, text=INI):
    path.write_text(text, encoding="utf-8")
    return path


# --- the contract with fsspec ---------------------------------------------------------------


def test_ia_is_registered_with_fsspec():
    """The installed fsspec must provide the protocol: nothing in warc2zip registers it any more."""
    import fsspec

    fs, path = fsspec.core.url_to_fs("ia://item/file.warc.gz")
    assert isinstance(fs, InternetArchiveFileSystem)
    assert path == "https://archive.org/download/item/file.warc.gz"


def test_ia_input_names_the_zip_like_any_other_uri():
    assert input_basename("ia://item/CRAWL-00032.warc.gz") == "CRAWL-00032.warc.gz"
    assert str(default_output_path("ia://item/CRAWL-00032.warc.gz", run_id="a1b2")) == "CRAWL-00032_a1b2.zip"


def test_ia_uri_converts_and_refetches(ia_server, warc_path, tmp_path):
    """main() streams the file by ranges through the redirect; the manifest carries the ia://
    URI verbatim, so --fetch can go back to it."""
    uri = ia_server.add("testitem", warc_path)
    out = tmp_path / "out.zip"
    assert main(uri, str(out)) == 0

    rows = manifest_rows(out)
    assert [row["warc_target_uri"] for row in rows] == [capture[0] for capture in CAPTURES]
    assert {row["source_uri"] for row in rows} == {uri}
    item_reads = [hit for hit in ia_server.hits if hit.path.startswith("/items/") and "Range" in hit.headers]
    assert item_reads, "data-node reads should be range requests"
    assert all(hit.path.startswith(("/download/", "/items/")) for hit in ia_server.hits)

    subset = write_subset(tmp_path / "subset.csv", [rows[0], rows[2]])
    fetched_path = tmp_path / "subset.warc.gz"
    assert fetch_main(str(subset), str(fetched_path)) == 0
    raw = warc_path.read_bytes()
    offset, length = warcinfo_range(out)
    fetched = fetched_path.read_bytes()
    assert fetched.startswith(raw[offset : offset + length])
    records = parsed_records(fetched)
    assert [t for t, _, _ in records] == ["warcinfo", "response", "response"]
    for record, row in zip(records[1:], [rows[0], rows[2]]):
        assert_is_stamped_copy(record, parsed_records(slice_of(raw, row))[0], uri, row)


def test_logged_in_user_converts_a_restricted_item(ia_server, warc_path, tmp_path, monkeypatch):
    """The issue's acceptance: anonymous, a restricted item is refused; once ia.ini holds the
    account's cookies it converts, with nothing passed on the command line. (How the cookies
    reach the data node behind the cross-origin redirect is fsspec's business and tested there.)"""
    uri = ia_server.add("restricted", warc_path)
    ia_server.require_cookie = ("logged-in-sig", "1756000000-abcdef0123456789")
    with pytest.raises(PermissionError):
        main(uri, str(tmp_path / "anonymous.zip"))

    monkeypatch.setenv("IA_CONFIG_FILE", str(write_ini(tmp_path / "ia.ini")))
    InternetArchiveFileSystem.clear_instance_cache()
    out = tmp_path / "out.zip"
    assert main(uri, str(out)) == 0
    assert len(manifest_rows(out)) == len(CAPTURES)


def test_cli_explains_a_refused_ia_item(ia_server, warc_path, tmp_path, monkeypatch, capsys):
    uri = ia_server.add("restricted", warc_path)
    ia_server.require_cookie = ("logged-in-sig", "nope")
    monkeypatch.setattr("sys.argv", ["warc2zip", uri, "--output", str(tmp_path / "out.zip")])
    assert cli() == 1
    err = capsys.readouterr().err
    assert "refused access to item 'restricted'" in err
    assert "anonymous" in err and "ia configure" in err


def test_permission_message_names_the_credentials_it_used(tmp_path):
    anonymous = format_ia_permission_error("ia://item/x.warc.gz", load_ia_credentials(str(tmp_path / "none.ini")))
    assert "anonymous" in anonymous and "no ia.ini" in anonymous
    logged_in = format_ia_permission_error("ia://item/x.warc.gz", load_ia_credentials(str(write_ini(tmp_path / "ia.ini"))))
    assert "were refused" in logged_in and str(tmp_path / "ia.ini") in logged_in
    assert "https://archive.org/download/item/x.warc.gz" in logged_in


def test_permission_error_elsewhere_is_not_blamed_on_archive_org(tmp_path, monkeypatch):
    monkeypatch.setattr(warc2zip, "main", lambda *a, **k: (_ for _ in ()).throw(PermissionError("disk")))
    monkeypatch.setattr("sys.argv", ["warc2zip", "local.warc.gz"])
    with pytest.raises(PermissionError, match="disk"):
        cli()
