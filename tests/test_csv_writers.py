"""Tests for the CSV metadata writers.

The values here are not hypothetical: every pathological case is one that actually shows up in
Common Crawl WARCs. The NUL case (`connection: close\x00`, CC-MAIN-2026-21) is the one that made
an entire 37 MB response_http_headers.csv unreadable by csv.reader.
"""

import csv
import io

import pytest

from warc2zip import sanitize_csv_value, write_denormalized_csv, write_multiline_csv

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
