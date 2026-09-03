"""Tests for the CSV metadata writers.

The values here are not hypothetical: every pathological case is one that actually shows up in
Common Crawl WARCs. The NUL case (`connection: close\x00`, CC-MAIN-2026-21) is the one that made
an entire 37 MB response_http_headers.csv unreadable by csv.reader.
"""

import csv
import io

import pytest

from warc2zip import (
    MANIFEST_COLUMNS,
    flatten_body_rows,
    parse_warc_fields,
    record_location_pairs,
    request_line_pairs,
    sanitize_csv_value,
    write_denormalized_csv,
    write_manifest_csv,
    write_multiline_csv,
)

# A real Cloudflare report_to value: quote-heavy JSON inside a header.
REPORT_TO = '{"group":"cf-nel","max_age":604800,"endpoints":[{"url":"https://a.nel.cloudflare.com/report/v4"}]}'

PATHOLOGICAL_VALUES = [
    "close\x00",  # NUL, seen on a real capture
    REPORT_TO,  # embedded double quotes
    'cfCacheStatus;desc="DYNAMIC"',  # server-timing, trailing quote
    "no-store, must-revalidate, no-cache",  # embedded delimiter
    "a\r\n b",  # obs-fold continuation
    "gzip\tdeflate",  # TAB, kept verbatim
    "Ünïcødé — 日本語",  # non-ASCII
    "C:\\path\\to\\thing",  # literal backslashes
    "\x1b[31mred\x1b[0m",  # ANSI escape
    "",  # empty value
]


def parse(content):
    """Parse with the stock reader — the whole point is that no special dialect is needed."""
    return list(csv.reader(io.StringIO(content)))


def test_denormalized_round_trips_pathological_values():
    rows = [(f"100000{i}.html", "some-header", value) for i, value in enumerate(PATHOLOGICAL_VALUES)]

    content, skipped = write_denormalized_csv(rows)

    assert skipped == 0
    parsed = parse(content)
    assert parsed[0] == ["filename", "header_name", "header_value"]
    assert len(parsed) == len(rows) + 1
    assert all(len(row) == 3 for row in parsed)


def test_multiline_round_trips_pathological_values():
    pairs = [("Some-Header", value) for value in PATHOLOGICAL_VALUES]

    content, skipped = write_multiline_csv([("1000000.html", pairs)])

    assert skipped == 0
    parsed = parse(content)
    assert parsed[0] == ["filename", "headers"]
    assert len(parsed) == 2
    filename, headers = parsed[1]
    assert filename == "1000000.html"
    # One line per header: the join newline survives, the ones inside values do not.
    assert len(headers.split("\n")) == len(pairs)


def test_nul_is_escaped_not_dropped():
    content, _ = write_denormalized_csv([("1014396.html", "Connection", "close\x00")])

    assert "\x00" not in content
    assert parse(content)[1] == ["1014396.html", "connection", "close\\x00"]


def test_embedded_newlines_do_not_split_a_row():
    content, _ = write_denormalized_csv([("1000000.html", "X-Fold", "a\r\n b")])

    assert parse(content)[1] == ["1000000.html", "x_fold", "a\\x0d\\x0a b"]
    assert len(content.rstrip("\r\n").split("\r\n")) == 2  # header row + one data row


def test_quotes_are_doubled_not_backslash_escaped():
    content, _ = write_denormalized_csv([("1000000.html", "Report-To", REPORT_TO)])

    assert '""group""' in content
    assert parse(content)[1][2] == REPORT_TO


def test_backslashes_are_left_alone():
    """Guards against re-introducing an escapechar, which would double every backslash."""
    content, _ = write_denormalized_csv([("1000000.html", "X-Path", "C:\\path\\to\\thing")])

    assert parse(content)[1][2] == "C:\\path\\to\\thing"


def test_header_names_are_normalized_but_values_are_not():
    content, _ = write_denormalized_csv([("1000000.html", "  Content-Type  ", "  text/html  ")])

    assert parse(content)[1] == ["1000000.html", "content_type", "  text/html  "]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain", "plain"),
        ("close\x00", "close\\x00"),
        ("a\r\nb", "a\\x0d\\x0ab"),
        ("keep\ttab", "keep\ttab"),
        ("del\x7f", "del\\x7f"),
        (None, "None"),
        (42, "42"),
    ],
)
def test_sanitize_csv_value(value, expected):
    assert sanitize_csv_value(value) == expected


def test_empty_input_still_writes_a_header_row():
    assert write_denormalized_csv([]) == ('"filename","header_name","header_value"\r\n', 0)
    assert write_multiline_csv([]) == ('"filename","headers"\r\n', 0)


def test_bad_row_is_skipped_and_counted(capsys):
    class Exploding:
        """A value that cannot be rendered, standing in for an unwritable header."""

        def __str__(self):
            raise UnicodeError("boom")

    rows = [
        ("1000000.html", "ok-header", "fine"),
        ("1000001.html", "bad-header", Exploding()),
        ("1000002.html", "ok-header", "also fine"),
    ]

    content, skipped = write_denormalized_csv(rows, label="response_http_headers.csv")

    assert skipped == 1
    assert len(parse(content)) == 3  # header row + the two good rows
    err = capsys.readouterr().err
    assert "bad_header" in err and "1000001.html" in err


# --- warc-fields parsing -----------------------------------------------------------------

# The shape CC actually writes into a metadata record.
CLD2 = '{"reliable":true,"languages":[{"code":"zh","text-covered":0.87,"name":"Chinese"}]}'


def test_warc_fields_parses_crlf_and_lf_alike():
    assert parse_warc_fields("a: 1\r\nb: 2\r\n") == [("a", "1"), ("b", "2")]
    assert parse_warc_fields("a: 1\nb: 2\n") == [("a", "1"), ("b", "2")]


def test_warc_fields_skips_blank_lines():
    assert parse_warc_fields("a: 1\n\n\nb: 2\n") == [("a", "1"), ("b", "2")]


def test_warc_fields_splits_on_the_first_colon_only():
    """CC's http-header-user-agent carries colons inside its value."""
    body = "http-header-user-agent: Mozilla/5.0 (X11; Linux) time: 12:30\r\n"
    assert parse_warc_fields(body) == [("http-header-user-agent", "Mozilla/5.0 (X11; Linux) time: 12:30")]


def test_warc_fields_joins_obs_fold_continuations():
    assert parse_warc_fields("a: one\r\n  two\r\n\tthree\r\n") == [("a", "one two three")]


def test_warc_fields_keeps_duplicate_names():
    assert parse_warc_fields("a: 1\na: 2\n") == [("a", "1"), ("a", "2")]


def test_warc_fields_keeps_json_values_opaque():
    assert parse_warc_fields(f"languages-cld2: {CLD2}\n") == [("languages-cld2", CLD2)]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\n",
        "not a field list at all",
        '{"just": "json"}',
        "  leading continuation with no field before it\n",
        ": no name\n",
    ],
)
def test_warc_fields_refuses_bodies_that_are_not_field_lists(body):
    assert parse_warc_fields(body) is None


# --- shallow flattening ------------------------------------------------------------------


def test_flatten_prefixes_lowercases_and_underscores():
    rows = flatten_body_rows("fetchTimeMs: 185\r\ncharset-detected: UTF-8\r\n")
    assert rows == [("_body.fetchtimems", "185"), ("_body.charset_detected", "UTF-8")]


def test_flatten_leaves_nested_json_as_one_value():
    assert flatten_body_rows(f"languages-cld2: {CLD2}\n") == [("_body.languages_cld2", CLD2)]


def test_flatten_falls_back_to_one_raw_row_for_a_non_field_body():
    assert flatten_body_rows("just some prose") == [("_body", "just some prose")]


def test_flatten_honours_a_custom_prefix():
    assert flatten_body_rows("software: nutch\n", prefix="_info") == [("_info.software", "nutch")]


# --- request line ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("GET / HTTP/1.1", [("request_method", "GET"), ("request_target", "/")]),
        ("POST /a/b?c=1 HTTP/1.1", [("request_method", "POST"), ("request_target", "/a/b?c=1")]),
        # tolerated: the protocol-first shape earlier versions composed
        ("HTTP/1.1 GET /", [("request_method", "GET"), ("request_target", "/")]),
        ("", []),
        ("GET", []),
    ],
)
def test_request_line_pairs(line, expected):
    assert request_line_pairs(line) == expected


# --- wide manifest CSV -------------------------------------------------------------------


def test_manifest_csv_round_trips_a_comma_and_quote_heavy_uri():
    entry = {
        "filename": "1000000.html",
        "warc_type": "response",
        "warc_record_id": "<urn:uuid:abc>",
        "warc_target_uri": 'https://example.com/a,b?q="x"',
        "warc_date": "2026-05-08T07:59:02Z",
        "http_status_code": "200",
        "detected_mime_type": "text/html",
        "content_type_header": "text/html; charset=UTF-8",
        "payload_size": 299529,
        "warc_refers_to_target_uri": "",
        "warc_refers_to_date": "",
        "warc_filename": "CC-MAIN-20260618163205-20260618193205-00999.warc.gz",
        "source_uri": "https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-25/segments/x/warc/y.warc.gz",
        "warc_record_offset": 1048576,
        "warc_record_length": 9636,
    }

    content, skipped = write_manifest_csv([entry])
    rows = parse(content)

    assert skipped == 0
    assert rows[0] == list(MANIFEST_COLUMNS)
    assert rows[1] == [str(entry[column]) for column in MANIFEST_COLUMNS]


def test_manifest_csv_sanitizes_control_characters():
    entry = dict.fromkeys(MANIFEST_COLUMNS, "")
    entry["content_type_header"] = "text/html\x00".replace("\\x00", "\x00")

    content, _ = write_manifest_csv([entry])

    assert "\x00" not in content
    assert parse(content)[1][MANIFEST_COLUMNS.index("content_type_header")] == "text/html\\x00"


def test_manifest_csv_tolerates_a_missing_key():
    content, skipped = write_manifest_csv([{"filename": "1000000.html"}])

    assert skipped == 0
    assert parse(content)[1] == ["1000000.html"] + [""] * (len(MANIFEST_COLUMNS) - 1)


# --- record location ---------------------------------------------------------------------


def test_record_location_pairs_stringifies_both_numbers():
    assert record_location_pairs(1048576, 9636) == [
        ("warc_record_offset", "1048576"),
        ("warc_record_length", "9636"),
    ]


def test_record_location_pairs_are_emitted_for_offset_zero():
    """The first record in a WARC sits at offset 0 — a falsy value that must not be dropped."""
    assert record_location_pairs(0, 487) == [("warc_record_offset", "0"), ("warc_record_length", "487")]


@pytest.mark.parametrize("offset, length", [(None, 9636), (1048576, None), (None, None)])
def test_record_location_pairs_omitted_when_unknown(offset, length):
    assert record_location_pairs(offset, length) == []
