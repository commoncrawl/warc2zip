"""ia://item/file inputs: the Internet Archive filesystem, its credentials, and the two paths
through it (a conversion with main(), a re-fetch with fetch_main()).

Everything runs offline against the `ia_server` fixture, which reproduces archive.org's shape: a
/download/ URL that 302s to a data node on another origin, serving byte ranges, optionally only
to a logged-in cookie. The one network test lives in test_readme_warcs.py.
"""

import pytest
from conftest import CAPTURES
from test_fetch import assert_is_stamped_copy, manifest_rows, parsed_records, slice_of, warcinfo_range, write_subset

import warc2zip
from warc2zip import (
    InternetArchiveFileSystem,
    cli,
    default_output_path,
    fetch_main,
    format_ia_permission_error,
    ia_config_path,
    input_basename,
    load_ia_credentials,
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


# --- the URI mapping ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    ["ia://EOT24PRE-crawl808/EOT24PRE-00032.warc.gz", "EOT24PRE-crawl808/EOT24PRE-00032.warc.gz"],
)
def test_ia_uri_maps_to_the_download_url_and_back(uri):
    url = InternetArchiveFileSystem._strip_protocol(uri)
    assert url == "https://archive.org/download/EOT24PRE-crawl808/EOT24PRE-00032.warc.gz"
    assert InternetArchiveFileSystem._strip_protocol(url) == url  # idempotent: fsspec strips twice
    fs = InternetArchiveFileSystem(cookies={})
    assert fs.unstrip_protocol(url) == "ia://EOT24PRE-crawl808/EOT24PRE-00032.warc.gz"


def test_ia_is_registered_with_fsspec():
    import fsspec

    fs, path = fsspec.core.url_to_fs("ia://item/file.warc.gz")
    assert isinstance(fs, InternetArchiveFileSystem)
    assert path == "https://archive.org/download/item/file.warc.gz"


def test_ia_input_names_the_zip_like_any_other_uri():
    assert input_basename("ia://item/CRAWL-00032.warc.gz") == "CRAWL-00032.warc.gz"
    assert str(default_output_path("ia://item/CRAWL-00032.warc.gz", run_id="a1b2")) == "CRAWL-00032_a1b2.zip"


# --- credentials ----------------------------------------------------------------------------


def test_no_ia_ini_means_anonymous():
    assert ia_config_path() is None
    credentials = load_ia_credentials()
    assert credentials.anonymous
    assert credentials.config_file is None
    fs = InternetArchiveFileSystem()
    assert fs.cookies == {} and fs.access_key is None
    assert "Authorization" not in fs.kwargs.get("headers", {})


def test_ia_ini_is_parsed_like_the_internetarchive_package(tmp_path, monkeypatch):
    ini = write_ini(tmp_path / "ia.ini")
    monkeypatch.setenv("IA_CONFIG_FILE", str(ini))
    assert ia_config_path() == str(ini)
    credentials = load_ia_credentials()
    assert (credentials.access_key, credentials.secret_key) == ("AKIA-TEST", "s3cr3t")
    # the cookie attributes in the file are not part of the value
    assert credentials.cookies == {
        "logged-in-user": "someone%40example.org",
        "logged-in-sig": "1756000000-abcdef0123456789",
    }
    assert not credentials.anonymous
    fs = InternetArchiveFileSystem()
    assert fs.kwargs["headers"]["Authorization"] == "LOW AKIA-TEST:s3cr3t"
    assert fs.cookies == credentials.cookies


def test_ia_ini_lookup_order(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg_default = home / ".config" / "internetarchive" / "ia.ini"
    dot_ia = home / ".ia"
    for path in (xdg_default, dot_ia):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_ini(path)
    assert ia_config_path() == str(xdg_default)
    xdg_default.unlink()
    assert ia_config_path() == str(dot_ia)
    (tmp_path / "elsewhere").mkdir()
    other = write_ini(tmp_path / "elsewhere" / "ia.ini")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere" / "internetarchive").mkdir()
    write_ini(tmp_path / "elsewhere" / "internetarchive" / "ia.ini")
    assert ia_config_path() == str(tmp_path / "elsewhere" / "internetarchive" / "ia.ini")
    monkeypatch.setenv("IA_CONFIG_FILE", str(other))
    assert ia_config_path() == str(other)


def test_environment_keys_override_the_file_and_come_in_pairs(tmp_path, monkeypatch):
    ini = write_ini(tmp_path / "ia.ini")
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "ENV-KEY")
    monkeypatch.setenv("IA_SECRET_ACCESS_KEY", "ENV-SECRET")
    credentials = load_ia_credentials(str(ini))
    assert (credentials.access_key, credentials.secret_key) == ("ENV-KEY", "ENV-SECRET")
    assert credentials.cookies  # the file's cookies are still used
    monkeypatch.delenv("IA_SECRET_ACCESS_KEY")
    with pytest.raises(ValueError, match="must be set together"):
        load_ia_credentials(str(ini))


def test_a_lone_key_in_the_file_is_ignored(tmp_path):
    ini = write_ini(tmp_path / "ia.ini", "[s3]\naccess = only-half\n[cookies]\n")
    credentials = load_ia_credentials(str(ini))
    assert credentials.anonymous
    assert credentials.config_file == str(ini)


def test_explicit_arguments_win_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_CONFIG_FILE", str(write_ini(tmp_path / "ia.ini")))
    fs = InternetArchiveFileSystem(cookies={"logged-in-sig": "explicit"})
    assert fs.cookies == {"logged-in-sig": "explicit"}
    assert "Authorization" not in fs.kwargs.get("headers", {})


# --- end to end against the stand-in server --------------------------------------------------


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


def test_login_cookies_survive_the_cross_origin_redirect(ia_server, warc_path, tmp_path, monkeypatch):
    """The data node is on another origin; aiohttp drops Cookie headers there, the jar does not."""
    uri = ia_server.add("restricted", warc_path)
    ia_server.require_cookie = ("logged-in-sig", "1756000000-abcdef0123456789")

    anonymous = InternetArchiveFileSystem(cookies={}, skip_instance_cache=True)
    with pytest.raises(PermissionError):
        anonymous.open(uri, "rb")
    with pytest.raises(PermissionError):
        anonymous.cat_file(uri, start=0, end=10)

    monkeypatch.setenv("IA_CONFIG_FILE", str(write_ini(tmp_path / "ia.ini")))
    InternetArchiveFileSystem.clear_instance_cache()
    ia_server.hits.clear()  # only the logged-in run's requests from here on
    out = tmp_path / "out.zip"
    assert main(uri, str(out)) == 0  # credentials picked up from the ini, no arguments passed
    assert len(manifest_rows(out)) == len(CAPTURES)
    first_hop = next(hit for hit in ia_server.hits if hit.path.startswith("/download/"))
    assert "logged-in-sig" not in first_hop.headers.get("Cookie", "")  # 127.0.0.1 is not `localhost`
    assert first_hop.headers.get("Authorization") == "LOW AKIA-TEST:s3cr3t"


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
