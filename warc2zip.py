import argparse
import csv
import io
import json
import mimetypes
import posixpath
import re
import secrets
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator
from warcio.recordloader import ARC2WARCHeadersParser
from warcio.utils import fsspec_open

MIME_EXTENSION_OVERRIDES = {
    "text/html": ".html",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    # ARC-era Heritrix wrote DNS lookups as their own records with this type. It has no
    # registered extension, and ".txt" would say less than the archive knows, so name it after
    # the type rather than after the fact that the body happens to be readable.
    "text/dns": ".dns",
}

# Payload kinds settled by the URL rather than by the Content-Type. Servers hand robots.txt out as
# text/plain, text/html, application/octet-stream and worse, and none of those say "this is an
# exclusion file" — the path does. Keyed on urlparse().path exactly: RFC 9309 puts the exclusion
# file at the site root and nowhere else, so "/help/robots.txt" is an ordinary page.
#
# Add other well-known *root* paths here as they come up (e.g. "/sitemap.xml"). Kinds that live at
# arbitrary URLs cannot be caught this way at all — see choose_extension().
PATH_EXTENSION_OVERRIDES = {
    "/robots.txt": ".robots",
}

# Pseudo-header carrying the content-type the ARC record itself declared. warcio's ARC->WARC
# mapping overwrites that field with "application/http;msgtype=response" (see _ARC2WARCKeepMime),
# and for the many ARC records with no HTTP layer at all — dns:, whois:, ntp: — it is the only
# mime information the archive has. Present on ARC-derived records only; its presence is also
# how the rest of this module recognises that the input was an ARC.
ARC_CONTENT_TYPE_HEADER = "ARC-Content-Type"

DRY_RUN_MAX = 10  # hard cap on capture records scanned by --dry-run

# The record types main() actually consumes, in the order the summary table lists them.
# Anything else (revisit, resource, conversion, continuation, ...) is read and discarded, and
# the table printed by format_record_type_summary() is the only place it shows up at all.
EXTRACTED_RECORD_TYPES = ("warcinfo", "response", "request", "metadata")


class CountingStream(io.IOBase):
    def __init__(self, raw_stream):
        self._stream = raw_stream
        self._bytes_read = 0

    def tell(self):
        """Acts as the progress tracker for progress bar libraries."""
        return self._bytes_read

    def read(self, size=-1):
        data = self._stream.read(size)
        if data:
            self._bytes_read += len(data)
        return data

    def readline(self, size=-1):
        data = self._stream.readline(size)
        if data:
            self._bytes_read += len(data)
        return data

    # Forward other essential methods to the underlying stream
    def readable(self):
        return getattr(self._stream, 'readable', lambda: True)()

    def close(self):
        self._stream.close()


class _ARC2WARCKeepMime(ARC2WARCHeadersParser):
    """warcio's ARC->WARC header mapper, minus the content-type amnesia.

    The stock parser rewrites parts[3] to "application/http;msgtype=response" so an ARC record
    looks like a WARC application/http envelope. That is right for the envelope and wrong for the
    archive: the ARC header line's fourth field is the only place a dns:/whois:/ntp: record — which
    has no HTTP layer to fall back on — ever states what it contains. Re-attach it under
    ARC_CONTENT_TYPE_HEADER so nothing is lost.
    """

    def _get_protocol_and_headers(self, headerline, parts):
        # Read before super(), which mutates parts[3] in place for non-filedesc records.
        arc_content_type = parts[3] if len(parts) > 3 else ""
        protocol, headers = super()._get_protocol_and_headers(headerline, parts)
        headers.append((ARC_CONTENT_TYPE_HEADER, arc_content_type))
        return protocol, headers


def open_archive_iterator(stream):
    """ArchiveIterator that understands ARC as well as WARC.

    arc2warc=True is what makes ARC input work at all: without it warcio exposes the raw ARC
    5-tuple ("uri", "archive-date", ...) and every WARC-Target-URI / WARC-Date lookup in this
    module quietly returns None. It also mints a WARC-Record-ID per record, which the grouping
    dict needs as a key — ARC/1.1 predates record IDs, so every record would otherwise collide
    under the single key None and only the last one would survive into the manifest.

    The flag costs pure-WARC input nothing but warcio's per-record format-caching shortcut.
    """
    iterator = ArchiveIterator(stream, arc2warc=True)
    iterator.loader.arc_parser = _ARC2WARCKeepMime()
    return iterator


@dataclass
class RecordGroup:
    payload_filename: str = ""
    payload_size: int = 0
    response_mime_type: str | None = None
    response_warc_headers: list[tuple[str, str]] = field(default_factory=list)
    response_http_headers: list[tuple[str, str]] = field(default_factory=list)
    response_target_uri: str = ""
    response_date: str = ""
    response_record_id: str = ""
    # True when response_record_id was minted by warcio for an ARC record rather than read from
    # the archive. Usable as a grouping key, but never written out: it is a fresh random UUID on
    # every run, and a CSV consumer would reasonably read it as identifying the source record.
    response_record_id_synthetic: bool = False
    http_status_code: str = ""
    content_type_header: str = ""
    response_order: int = -1
    response_offset: int | None = None  # byte offset of the response record in the WARC
    response_length: int | None = None  # byte length of that record (gzip member size)
    http_status_line: str = ""  # Full HTTP status line, e.g. "HTTP/1.1 200 OK"
    response_domain: str = ""  # Domain from WARC-Target-URI, e.g. "example.com"
    concurrent_to: str = ""  # WARC-Concurrent-To from the response record (points to request)
    requests: list[list[tuple[str, str]]] = field(default_factory=list)
    request_http_headers: list[list[tuple[str, str]]] = field(default_factory=list)
    request_http_lines: list[str] = field(default_factory=list)  # e.g. "GET /path HTTP/1.1"
    request_bodies: list[bytes] = field(default_factory=list)
    request_offsets: list[int | None] = field(default_factory=list)
    request_lengths: list[int | None] = field(default_factory=list)
    # (warc header pairs, body text, byte offset, byte length)
    metadata_entries: list[tuple[list[tuple[str, str]], str, int | None, int | None]] = field(default_factory=list)


def is_arc_record(record):
    """True when this record came from an ARC, i.e. warcio synthesized its WARC-* header names."""
    return record.rec_headers.get_header(ARC_CONTENT_TYPE_HEADER) is not None


def archive_header_pairs(record):
    """A record's headers, minus anything warcio invented that the archive never contained.

    For ARC input every WARC-* name is warcio's rename of a real ARC field — except
    WARC-Record-ID, which has no ARC counterpart at all and is a fresh random UUID on every run.
    Emitting it would invite a CSV consumer to read a per-run value as an identity, and it would
    break the --metadata-only invariant, which compares the metadata of two separate runs byte
    for byte.
    """
    pairs = list(record.rec_headers.headers)
    if not is_arc_record(record):
        return pairs
    return [(name, value) for name, value in pairs if name != "WARC-Record-ID"]


def normalize_mime_type(value):
    """Bare lowercase type from a Content-Type value, or "" if there isn't one."""
    if not value:
        return ""
    return value.split(";")[0].strip().lower()


def detect_mime_type(record):
    """Best available content-type for a record's payload.

    Two sources, and which one applies is decided by whether the record has an HTTP layer at all,
    never by whether that layer happened to declare a type:

      1. `record.http_headers` is not None — an HTTP transaction was captured. Its Content-Type is
         authoritative, **including when it is missing**: a capture whose server sent no
         Content-Type genuinely has no declared type, and saying so is the honest answer.
      2. `record.http_headers` is None — there was no HTTP transaction to parse, so field 4 of the
         ARC header line (ARC_CONTENT_TYPE_HEADER) is the only place the archive ever states what
         the payload contains. ARC's dns:/whois:/ntp: records are the case that matters; warcio
         also reports None for a zero-length record or a non-http(s) scheme, which are the same
         situation.

    The split is deliberately at `is None` rather than at "no Content-Type value". Letting the ARC
    type fill in for an HTTP layer that declared nothing would put a value in manifest.csv's
    detected_mime_type that its own content_type_header column cannot corroborate — and that column
    is how a CSV-only consumer checks the tool's work. Nothing is lost by the strict rule: the ARC
    declaration still reaches the output as an `arc_content_type` row in response_warc_headers.csv
    for every ARC record (see archive_header_pairs()), it is just not promoted into a column that
    cannot be cross-checked.
    """
    if record.http_headers is None:
        return normalize_mime_type(record.rec_headers.get_header(ARC_CONTENT_TYPE_HEADER)) or "application/octet-stream"
    return normalize_mime_type(record.http_headers.get_header("Content-Type")) or "application/octet-stream"


def mime_to_extension(mime_type):
    if mime_type in MIME_EXTENSION_OVERRIDES:
        return MIME_EXTENSION_OVERRIDES[mime_type]
    return mimetypes.guess_extension(mime_type) or ".unk"


def path_extension_override(target_uri):
    """Extension implied by a capture's URL alone, or None.

    Guards on netloc so an opaque scheme is never read as a path: urlparse("dns:ntsb.gov") puts
    the host in .path and leaves .netloc empty, and ARC input is full of those. A query string is
    tolerated ("/robots.txt?v=2" is the same resource, .query holds the rest); case is not, since
    the path is case-sensitive and a server is under no obligation to serve "/Robots.txt".
    """
    if not target_uri:
        return None
    parsed = urlparse(target_uri)
    if not parsed.netloc:
        return None
    return PATH_EXTENSION_OVERRIDES.get(parsed.path)


def choose_extension(mime_type, target_uri):
    """File extension for a payload: the URL where it knows better, else the declared type.

    The URL rule is deliberately unconditional — status and body length do not gate it. The
    extension records what was *requested*; whether the server actually served it is what
    http_status_code and payload_size are for in manifest.csv, and a consumer that cares must
    filter there anyway. In the EOT-2004 file that means 336 of 533 ".robots" files are in fact
    404 error pages, and the 5 zero-byte 200s — an empty robots.txt is a real allow-all answer,
    not a missing one — keep the extension they deserve.

    This mechanism only reaches kinds that live at a fixed, well-known path. Feeds do not: they
    sit at arbitrary URLs and are announced by <link rel="alternate">, so the thing that
    identifies them is their registered type (application/rss+xml, application/atom+xml) and
    they belong in MIME_EXTENSION_OVERRIDES instead. Sitemaps are half and half — "/sitemap.xml"
    is conventional enough for the table above, but the ones announced by a robots.txt "Sitemap:"
    line can be anywhere, and catching those would need the XML root element (<urlset> /
    <sitemapindex>), i.e. reading the payload rather than the headers.
    """
    return path_extension_override(target_uri) or mime_to_extension(mime_type)


# Every C0 control except TAB, plus DEL. TAB is harmless inside a quoted field and occurs
# legitimately in header values; NUL and CR/LF are not — see sanitize_csv_value().
CSV_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# The dialect is pinned field-by-field rather than inherited from csv.excel's class attributes,
# so a mutated global dialect can't silently change how values are quoted. No escapechar on
# purpose: QUOTE_ALL + doublequote covers every case, and setting one would double every
# backslash in a value (including the \xNN sequences sanitize_csv_value emits).
CSV_DIALECT = {
    "delimiter": ",",
    "quotechar": '"',
    "doublequote": True,
    "lineterminator": "\r\n",
    "quoting": csv.QUOTE_ALL,
}


def sanitize_csv_value(value):
    r"""Make control characters visible so CSV output stays reader-parseable.

    Header values off the wire can carry a NUL (seen in CC-MAIN-2026-21: `connection: close\x00`)
    or, via obs-fold, an embedded CR/LF. A single NUL makes csv.reader reject the whole file;
    rendering these as `\xNN` keeps rows single-line and the file parseable, without discarding
    the evidence that the byte was there.
    """
    if not isinstance(value, str):
        value = str(value)
    return CSV_CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", value)


def write_denormalized_csv(rows, label="csv"):
    """Write denormalized CSV: filename, header_name, header_value (multiple rows per file).

    Returns (content, skipped) — a row that still fails to write is reported and skipped rather
    than aborting a conversion that has already streamed the whole WARC.
    """
    sio = io.StringIO()
    writer = csv.writer(sio, **CSV_DIALECT)
    writer.writerow(["filename", "header_name", "header_value"])
    skipped = 0
    for row in rows:
        filename, header_name, header_value = row
        header_name = header_name.strip().lower().replace("-", "_")

        try:
            writer.writerow((filename, header_name, sanitize_csv_value(header_value)))
        except (csv.Error, UnicodeError) as e:
            skipped += 1
            print(f"warning: {label}: skipped header {header_name!r} of {filename}: {e}", file=sys.stderr)
    return sio.getvalue(), skipped


def write_multiline_csv(rows, label="csv"):
    """Write multiline CSV: filename, headers (one row per file, headers as multiline string).

    Returns (content, skipped) — see write_denormalized_csv().
    """
    sio = io.StringIO()
    writer = csv.writer(sio, **CSV_DIALECT)
    writer.writerow(["filename", "headers"])
    skipped = 0
    for filename, header_pairs in rows:
        headers_str = "\n".join(
            f"{name.replace('-', '_').lower()}: {sanitize_csv_value(value)}" for name, value in header_pairs
        )
        try:
            writer.writerow([filename, headers_str])
        except (csv.Error, UnicodeError) as e:
            skipped += 1
            print(f"warning: {label}: skipped headers of {filename}: {e}", file=sys.stderr)
    return sio.getvalue(), skipped


# A warc-fields name is an HTTP token (RFC 9110 5.6.2). Checking the charset, not just the
# presence of a colon, is what stops a one-line JSON body ({"just": "json"}) from being read as a
# field named '{"just"' — a real risk, since some producers put JSON in a metadata record.
WARC_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def parse_warc_fields(text):
    r"""Parse an application/warc-fields body into (name, value) pairs.

    Returns None when the body doesn't look like warc-fields, so callers keep the raw block
    instead of mangling something that was never a field list (a JSON blob, free text).
    Only the first ':' splits a line: CC emits fields like
    `http-header-user-agent: Mozilla/5.0 (X11; Linux)` whose value carries its own colons.
    Duplicate names are kept as separate pairs, the way repeated WARC headers already are.
    """
    pairs = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        if line[0] in " \t":
            # obs-fold continuation of the previous field
            if not pairs:
                return None
            name, value = pairs[-1]
            pairs[-1] = (name, f"{value} {line.strip()}")
            continue
        name, sep, value = line.partition(":")
        if not sep or not WARC_FIELD_NAME_RE.match(name.strip()):
            return None
        pairs.append((name.strip(), value.strip()))
    return pairs or None


def flatten_body_rows(body_text, prefix="_body"):
    """Shallow-flatten a warc-fields body into ("_body.<field>", value) pairs.

    One level only: a field whose value is JSON (CC's languages-cld2) stays a single opaque
    value, because the list nested inside it doesn't flatten into columns usefully. Names are
    normalized here rather than left to the writers, since write_denormalized_csv() and
    write_multiline_csv() normalize slightly differently. A body that isn't warc-fields falls
    back to one raw row, so nothing is lost.
    """
    pairs = parse_warc_fields(body_text)
    if pairs is None:
        return [(prefix, body_text)]
    return [(f"{prefix}.{n.strip().lower().replace('-', '_')}", v) for n, v in pairs]


def record_location_pairs(offset, length):
    """Pseudo-headers locating a record inside the source WARC.

    Same triple a CDX index carries (filename, offset, length): with the WARC filename from
    manifest.csv, these two make a record re-fetchable with a single HTTP range request,
    because each record is its own gzip member.
    """
    if offset is None or length is None:
        return []
    return [("warc_record_offset", str(offset)), ("warc_record_length", str(length))]


def request_line_pairs(line):
    """Split an HTTP request line into request_method / request_target pseudo-header pairs.

    Expects "GET /path HTTP/1.1". A leading HTTP/x token is skipped rather than trusted, so a
    line assembled protocol-first can't turn into a row claiming the method is "HTTP/1.1".
    """
    parts = line.split()
    if parts and parts[0].upper().startswith("HTTP/"):
        parts = parts[1:]
    if len(parts) >= 2:
        return [("request_method", parts[0]), ("request_target", parts[1])]
    return []


MANIFEST_COLUMNS = (
    "filename",
    "warc_record_id",
    "warc_target_uri",
    "warc_date",
    "http_status_code",
    "detected_mime_type",
    "content_type_header",
    "payload_size",
    # Where this record came from: enough to re-fetch it from this row alone, with no join
    # against warcinfo.csv and no knowledge of the zip it was extracted from.
    "warc_filename",
    "source_uri",
    "warc_record_offset",
    "warc_record_length",
)


def write_manifest_csv(entries, label="manifest.csv"):
    """Write the wide CSV mirror of manifest.jsonl: one row per capture, one column per key.

    Fed from the same entry dicts as the JSONL so the two files cannot drift.
    Returns (content, skipped) - see write_denormalized_csv().
    """
    sio = io.StringIO()
    writer = csv.writer(sio, **CSV_DIALECT)
    writer.writerow(list(MANIFEST_COLUMNS))
    skipped = 0
    for entry in entries:
        try:
            writer.writerow([sanitize_csv_value(entry.get(column, "")) for column in MANIFEST_COLUMNS])
        except (csv.Error, UnicodeError) as e:
            skipped += 1
            print(f"warning: {label}: skipped entry {entry.get('filename')!r}: {e}", file=sys.stderr)
    return sio.getvalue(), skipped


def build_warcinfo_rows(warcinfos, source_uri=""):
    """Build denormalized and multiline CSV rows for the warcinfo record(s).

    warcinfo is crawl-level and belongs to no payload file, so the filename column carries a
    synthetic key: "warcinfo", then "warcinfo.1" etc. for the extra records a concatenated
    WARC brings along.

    `source_uri` is the input this zip was converted from, recorded as a synthetic row. The
    WARC's own WARC-Filename says what the file is called; this says where it was read from,
    which is what a range request actually needs. Emitted even when the WARC carries no
    warcinfo record at all, so the provenance is never lost.
    """
    rows = []
    multi = []
    for i, (headers, body_text, offset, length) in enumerate(warcinfos):
        key = "warcinfo" if i == 0 else f"warcinfo.{i}"
        pairs = record_location_pairs(offset, length) + list(headers)
        for n, v in pairs:
            rows.append((key, str.lower(n.replace("-", "_")), v))
        rows.extend((key, n, v) for n, v in flatten_body_rows(body_text))
        multi.append((key, pairs + [("_body", body_text)]))
    if source_uri:
        rows.insert(0, ("warcinfo", "source_uri", source_uri))
        if multi:
            multi[0] = (multi[0][0], [("source_uri", source_uri)] + multi[0][1])
        else:
            multi.append(("warcinfo", [("source_uri", source_uri)]))
    return rows, multi


def write_warcinfo_files(zip_file, root_dir, warcinfos):
    """Write the raw warcinfo record(s): WARC headers and warc-fields body, wire bytes preserved."""
    zip_file.writestr(
        f"{root_dir}/warcinfo.warc",
        "\n\n".join("\n".join(f"{n}: {v}" for n, v in headers) for headers, _body, _o, _l in warcinfos),
    )
    zip_file.writestr(
        f"{root_dir}/warcinfo.warc-fields",
        "\n\n".join(body for _headers, body, _o, _l in warcinfos),
    )


def format_record_type_summary(counts):
    """Aligned per-type table of every record read, extracted types first.

    Types outside EXTRACTED_RECORD_TYPES were read and discarded, and are flagged as such:
    a `resource` record with no metadata pointing at it leaves no other trace in the output.
    Zero-count types are omitted.
    """
    ordered = [t for t in EXTRACTED_RECORD_TYPES if counts.get(t)]
    ordered += sorted(t for t in counts if t not in EXTRACTED_RECORD_TYPES and counts[t])
    if not ordered:
        return "Record types: none"
    name_width = max(len(t) for t in ordered)
    count_width = max(len(str(counts[t])) for t in ordered)
    lines = ["Record types:"]
    for t in ordered:
        line = f"  {t:<{name_width}}  {counts[t]:>{count_width}}"
        if t not in EXTRACTED_RECORD_TYPES:
            line += "  (not extracted)"
        lines.append(line)
    return "\n".join(lines)


def get_file_size(input_file):
    """Get file size, handling both local paths and remote URIs."""
    try:
        return Path(input_file).stat().st_size
    except (OSError, ValueError):
        return None


def build_root_dir_name(crawl_name, partial=False):
    """Build a unique root directory name from a crawl name.

    Format: {crawl_name}_{YYYYMMDDTHHMMSS}_{4-char hex suffix}
    The suffix doesn't affect sort order since it comes after the timestamp.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(2) if not partial else secrets.token_hex(2) + "_partial"

    return f"{crawl_name}_{timestamp}_{suffix}"


def extract_crawl_name(warc_filename):
    """Extract a clean crawl name from a WARC-Filename header value.

    Strips path and extensions like .warc.gz to get a usable directory name. Longest suffix
    first, so .warc.gz is not left holding a stray ".warc".
    """
    name = posixpath.basename(warc_filename)
    for ext in (".warc.gz", ".warc", ".arc.gz", ".arc"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def build_group_metadata(group, warc_filename="", source_uri=""):
    """Build CSV rows and JSONL entry for a RecordGroup whose payload is already in the zip."""
    payload_filename = group.payload_filename

    # Response WARC headers, led by the record's location in the source WARC
    response_warc_pairs = record_location_pairs(group.response_offset, group.response_length)
    response_warc_pairs += group.response_warc_headers
    response_warc_rows = [(payload_filename, str.lower(n), v) for n, v in response_warc_pairs]
    response_warc_multi = (payload_filename, response_warc_pairs)

    # Response HTTP headers, led by a synthetic status_code row so the status is greppable.
    # Deliberately not the status line: CC's WARCs claim HTTP/1.1 even for h2 captures. The true
    # protocol is already in response_warc_headers.csv as warc_protocol rows.
    status_pairs = [("status_code", group.http_status_code)] if group.http_status_code else []
    response_http_pairs = status_pairs + group.response_http_headers
    # A capture with no HTTP layer at all still gets one blank row, so both response_http views
    # carry every payload file and a consumer filtering manifest.csv down to a subset can join on
    # `filename` without hitting a gap. ARC's dns:/whois:/ntp: records are the real case: they are
    # complete captures that simply never had an HTTP transaction, so "no headers" is the answer,
    # not a missing record. The multiline CSV already emitted a blank row here; this keeps the
    # denormalized one from disagreeing about which files exist.
    response_http_rows = (
        [(payload_filename, str.lower(n), v) for n, v in response_http_pairs]
        if response_http_pairs
        else [(payload_filename, "", "")]
    )
    response_http_multi = (payload_filename, response_http_pairs)

    # Request WARC and HTTP headers (all requests use the response's payload_filename).
    # The HTTP side used to reach sidecar files only, so flat format lost it entirely.
    request_warc_rows = []
    request_warc_multi_entries = []
    request_http_rows = []
    request_http_multi_entries = []
    for i, req_pairs in enumerate(group.requests):
        offset = group.request_offsets[i] if i < len(group.request_offsets) else None
        length = group.request_lengths[i] if i < len(group.request_lengths) else None
        req_pairs = record_location_pairs(offset, length) + req_pairs
        for n, v in req_pairs:
            request_warc_rows.append((payload_filename, str.lower(n.replace("-", "_")), v))
        request_warc_multi_entries.append((payload_filename, req_pairs))

        http_pairs = group.request_http_headers[i] if i < len(group.request_http_headers) else []
        request_line = group.request_http_lines[i] if i < len(group.request_http_lines) else ""
        http_pairs = request_line_pairs(request_line) + http_pairs
        if http_pairs:
            for n, v in http_pairs:
                request_http_rows.append((payload_filename, str.lower(n.replace("-", "_")), v))
            request_http_multi_entries.append((payload_filename, http_pairs))

    # Metadata entries (all use the response's payload_filename). The denormalized CSV gets the
    # warc-fields body shallow-flattened so each field can be grepped; the multiline CSV keeps the
    # raw block, so flat-format output still reproduces the wire bytes despite having no
    # .metadata.warc-fields sidecar.
    metadata_rows = []
    metadata_multi_entries = []
    for meta_pairs, body_text, offset, length in group.metadata_entries:
        meta_pairs = record_location_pairs(offset, length) + meta_pairs
        for n, v in meta_pairs:
            metadata_rows.append((payload_filename, str.lower(n.replace("-", "_")), v))
        metadata_rows.extend((payload_filename, n, v) for n, v in flatten_body_rows(body_text))
        all_pairs = meta_pairs + [("_body", body_text)]
        metadata_multi_entries.append((payload_filename, all_pairs))

    # JSONL manifest entry
    jsonl_entry = {
        "filename": payload_filename,
        # Blank rather than a per-run random UUID when the source was an ARC: the field does not
        # exist in ARC/1.1. warc_record_offset/length still make the row re-fetchable on its own.
        "warc_record_id": "" if group.response_record_id_synthetic else (group.response_record_id or ""),
        "warc_target_uri": group.response_target_uri,
        "warc_date": group.response_date,
        "http_status_code": group.http_status_code,
        "detected_mime_type": group.response_mime_type or "application/octet-stream",
        "content_type_header": group.content_type_header,
        "payload_size": group.payload_size,
        "warc_filename": warc_filename,
        "source_uri": source_uri,
        "warc_record_offset": "" if group.response_offset is None else group.response_offset,
        "warc_record_length": "" if group.response_length is None else group.response_length,
    }

    return {
        "response_warc_rows": response_warc_rows,
        "response_warc_multi": response_warc_multi,
        "response_http_rows": response_http_rows,
        "response_http_multi": response_http_multi,
        "request_warc_rows": request_warc_rows,
        "request_warc_multi": request_warc_multi_entries,
        "request_http_rows": request_http_rows,
        "request_http_multi": request_http_multi_entries,
        "metadata_rows": metadata_rows,
        "metadata_multi": metadata_multi_entries,
        "jsonl_entry": jsonl_entry,
    }


def write_sidecar_files(zip_file, root_dir, group):
    """Write per-file sidecar metadata for a single capture (sidecar format only)."""
    base = f"{root_dir}/{group.response_domain}/{group.payload_filename}"

    # Response WARC headers
    zip_file.writestr(
        f"{base}.response.warc",
        "\n".join(f"{n}: {v}" for n, v in group.response_warc_headers),
    )

    # Response HTTP headers (with status line)
    if group.response_http_headers:
        lines = []
        if group.http_status_line:
            lines.append(group.http_status_line)
        lines.extend(f"{n}: {v}" for n, v in group.response_http_headers)
        zip_file.writestr(f"{base}.response.http", "\n".join(lines))

    # Request sidecars (merged if multiple request records)
    if group.requests:
        warc_parts = []
        http_parts = []
        body_parts = []
        for i, warc_hdrs in enumerate(group.requests):
            warc_parts.append("\n".join(f"{n}: {v}" for n, v in warc_hdrs))
            if i < len(group.request_http_headers):
                http_lines = []
                if i < len(group.request_http_lines) and group.request_http_lines[i]:
                    http_lines.append(group.request_http_lines[i])
                http_lines.extend(f"{n}: {v}" for n, v in group.request_http_headers[i])
                http_parts.append("\n".join(http_lines))
            if i < len(group.request_bodies):
                body_parts.append(group.request_bodies[i])
        zip_file.writestr(f"{base}.request.warc", "\n\n".join(warc_parts))
        if http_parts:
            zip_file.writestr(f"{base}.request.http", "\n\n".join(http_parts))
        if any(body_parts):
            zip_file.writestr(f"{base}.request.json", b"\n\n".join(body_parts))

    # Metadata sidecars (merged if multiple metadata records)
    if group.metadata_entries:
        warc_parts = []
        body_parts = []
        for meta_headers, body_text, _offset, _length in group.metadata_entries:
            warc_parts.append("\n".join(f"{n}: {v}" for n, v in meta_headers))
            body_parts.append(body_text)
        zip_file.writestr(f"{base}.metadata.warc", "\n\n".join(warc_parts))
        zip_file.writestr(f"{base}.metadata.warc-fields", "\n\n".join(body_parts))


def main(input_file, output_path, dry_run=False, limit=None, output_format="flat", metadata_only=False):
    file_size = get_file_size(input_file)

    if dry_run:
        capped = limit is None or limit > DRY_RUN_MAX
        if capped:
            limit = DRY_RUN_MAX
            print(
                f"Warning: --dry-run scans at most {DRY_RUN_MAX} capture records.",
                file=sys.stderr,
            )

        record_types = Counter()
        sample_uris = []
        sample_mimes = set()
        limit_reached = False

        with fsspec_open(input_file, "rb", default_fh=sys.stdin.buffer) as stream:
            if not hasattr(stream, "tell") or stream == sys.stdin.buffer:
                # sys.stdin.buffer has a tell() method but it crashes
                stream = CountingStream(stream)
            with tqdm(total=file_size, unit="B", unit_scale=True, desc="Scanning") as pbar:
                for record in open_archive_iterator(stream):
                    if limit_reached and record.rec_type == "response":
                        break
                    record_types[record.rec_type] += 1
                    record.content_stream().read()
                    if record.rec_type == "response":
                        uri = record.rec_headers.get_header("WARC-Target-URI") or "unknown"
                        mime = detect_mime_type(record)
                        if len(sample_uris) < 5:
                            sample_uris.append(uri)
                        sample_mimes.add(mime)
                        if limit is not None and record_types["response"] >= limit:
                            limit_reached = True
                    pbar.update(stream.tell() - pbar.n)

        if not limit_reached:
            limit_note = ""
        elif capped:
            limit_note = f" (stopped at the --dry-run cap of {limit})"
        else:
            limit_note = f" (stopped at --limit {limit})"
        print(
            f"[dry-run] {record_types['response']} responses, {record_types['request']} requests, "
            f"{record_types['metadata']} metadata records{limit_note}"
        )
        print(f"[dry-run] Sample URIs: {sample_uris}")
        print(f"[dry-run] Detected mime-types: {sorted(sample_mimes)}")
        return

    groups = {}  # response WARC-Record-ID -> RecordGroup
    pending_requests = {}  # request WARC-Record-ID -> list of header tuples
    order_counter = 0
    counter = 1_000_000
    root_dir = None  # resolved from first warcinfo record
    warcinfos = []  # list[(warc_header_pairs, body_text, offset, length)] - crawl-level provenance
    warc_filename = ""  # WARC-Filename from the warcinfo record; every manifest row repeats it

    # Fallback crawl name from input filename
    input_basename = posixpath.basename(input_file.rstrip("/"))
    fallback_crawl_name = extract_crawl_name(input_basename) if input_basename else "unknown"

    record_types = Counter()  # every record read, by WARC-Type — extracted or not
    limit_reached = False

    # response -> Record id  <-> metadata -
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as outer_zip:
        # Pass 1: Read WARC, write payloads immediately, buffer only headers
        with fsspec_open(input_file, "rb", default_fh=sys.stdin.buffer) as stream:
            if not hasattr(stream, "tell") or stream == sys.stdin.buffer:
                # sys.stdin.buffer has a tell() method but it crashes
                stream = CountingStream(stream)
            pbar = tqdm(total=file_size, unit="B", unit_scale=True, desc="Reading WARC")
            # Held by name rather than iterated anonymously: get_record_offset() /
            # get_record_length() hang off the iterator, not the record.
            record_iter = open_archive_iterator(stream)
            for record in record_iter:
                rec_type = record.rec_type

                if limit_reached and rec_type == "response":
                    break
                # Counted after the --limit break so the unconsumed next response stays out,
                # and before the warcinfo `continue` so every record read is in the table.
                record_types[rec_type] += 1

                if rec_type == "warcinfo":
                    body_text = record.content_stream().read().decode("utf-8", errors="replace")
                    warcinfos.append(
                        (
                            archive_header_pairs(record),
                            body_text,
                            record_iter.get_record_offset(),
                            record_iter.get_record_length(),
                        )
                    )
                    if root_dir is None:
                        warc_filename = record.rec_headers.get_header("WARC-Filename") or ""
                        crawl_name = extract_crawl_name(warc_filename) if warc_filename else fallback_crawl_name
                        root_dir = build_root_dir_name(crawl_name, limit is not None)
                    pbar.update(stream.tell() - pbar.n)
                    continue

                # Resolve root_dir before first payload write if no warcinfo appeared
                if root_dir is None:
                    root_dir = build_root_dir_name(fallback_crawl_name, limit is not None)

                if rec_type == "response":
                    record_id = record.rec_headers.get_header("WARC-Record-ID")
                    target_uri = record.rec_headers.get_header("WARC-Target-URI")
                    # ARC has no record IDs; warcio mints one per record so the grouping dict
                    # below still gets a unique key. Flagged so it never reaches a CSV.
                    is_arc = is_arc_record(record)

                    group = groups.setdefault(record_id, RecordGroup())
                    group.concurrent_to = record.rec_headers.get_header("WARC-Concurrent-To") or ""
                    payload = record.content_stream().read()
                    group.response_mime_type = detect_mime_type(record)
                    ext = choose_extension(group.response_mime_type, target_uri or "")
                    payload_filename = f"{counter}{ext}"

                    # Extract domain for sidecar format directory grouping
                    domain = urlparse(target_uri).netloc if target_uri else "unknown"
                    group.response_domain = domain or "unknown"

                    if output_format == "sidecar":
                        zip_path = f"{root_dir}/{group.response_domain}/{payload_filename}"
                    else:
                        zip_path = f"{root_dir}/{payload_filename}"
                    if not metadata_only:
                        outer_zip.writestr(zip_path, payload)

                    group.payload_filename = payload_filename
                    group.payload_size = len(payload)
                    group.response_record_id = record_id
                    group.response_record_id_synthetic = is_arc
                    group.response_target_uri = target_uri or ""
                    group.response_date = record.rec_headers.get_header("WARC-Date") or ""
                    group.response_warc_headers = archive_header_pairs(record)
                    if record.http_headers:
                        group.response_http_headers = list(record.http_headers.headers)
                        group.content_type_header = record.http_headers.get_header("Content-Type") or ""
                        group.http_status_code = str(record.http_headers.get_statuscode() or "")
                        group.http_status_line = f"{record.http_headers.protocol} {record.http_headers.statusline}"
                    group.response_offset = record_iter.get_record_offset()
                    group.response_length = record_iter.get_record_length()
                    group.response_order = order_counter
                    order_counter += 1
                    counter += 1
                    if limit is not None and record_types["response"] >= limit:
                        limit_reached = True

                elif rec_type == "request":
                    body = record.content_stream().read()
                    request_record_id = record.rec_headers.get_header("WARC-Record-ID")
                    req_entry = {
                        "warc_headers": archive_header_pairs(record),
                        "http_headers": list(record.http_headers.headers) if record.http_headers else [],
                        # warcio's parser splits a request line as protocol="GET",
                        # statusline="/path HTTP/1.1" — recompose it to get "GET /path HTTP/1.1".
                        "http_request_line": f"{record.http_headers.protocol} {record.http_headers.statusline}"
                        if record.http_headers
                        else "",
                        "body": body,
                        "offset": record_iter.get_record_offset(),
                        "length": record_iter.get_record_length(),
                    }
                    pending_requests.setdefault(request_record_id, []).append(req_entry)

                elif rec_type == "metadata":
                    body = record.content_stream().read()
                    concurrent_to = record.rec_headers.get_header("WARC-Concurrent-To")

                    group = groups.setdefault(concurrent_to, RecordGroup())
                    body_text = body.decode("utf-8", errors="replace")
                    group.metadata_entries.append(
                        (
                            archive_header_pairs(record),
                            body_text,
                            record_iter.get_record_offset(),
                            record_iter.get_record_length(),
                        )
                    )

                else:
                    record.content_stream().read()

                pbar.update(stream.tell() - pbar.n)
            pbar.close()

        # Link pending requests to their response groups via WARC-Concurrent-To
        linked_request_ids = set()
        for group in groups.values():
            if group.concurrent_to and group.concurrent_to in pending_requests:
                linked_request_ids.add(group.concurrent_to)
                for req in pending_requests[group.concurrent_to]:
                    group.requests.append(req["warc_headers"])
                    group.request_http_headers.append(req["http_headers"])
                    group.request_http_lines.append(req["http_request_line"])
                    group.request_bodies.append(req["body"])
                    group.request_offsets.append(req["offset"])
                    group.request_lengths.append(req["length"])

        # Two audits feed one warning, because they see different things. A group is only ever
        # created by a response or a metadata record, so orphan groups are metadata-anchored
        # captures whose anchor was not a `response` (a revisit, on Common Crawl). Requests are
        # joined from the response side and never create a group, so a request whose response
        # never appeared — or was a revisit — is invisible to the first audit and only the
        # second sees it. The two record counts are exact and run in parallel with the capture
        # count (each dropped capture loses its request AND its metadata record) — they are not
        # a partition of it, which is why the message says "along with their". The capture count
        # is the best available figure, since nothing links an unclaimed request to an orphan
        # group (on CC they are the same captures, on a producer with no metadata records only
        # requests exist). Either way the whole capture is dropped from every output file, not
        # just the payload: pass 2 below iterates only groups with a payload_filename.
        orphan_groups = [g for g in groups.values() if not g.payload_filename]
        dropped_metadata = sum(len(g.metadata_entries) for g in orphan_groups)
        unlinked_requests = sum(len(v) for k, v in pending_requests.items() if k not in linked_request_ids)
        if orphan_groups or unlinked_requests:
            print(
                f"Warning: skipped {max(len(orphan_groups), unlinked_requests)} capture(s) that have no "
                f"response record (e.g. revisits), along with their {unlinked_requests} request and "
                f"{dropped_metadata} metadata records",
                file=sys.stderr,
            )

        # A WARC without a warcinfo record, or without WARC-Filename on it, still needs a name
        # in the manifest: fall back to what the input was called.
        warc_filename = warc_filename or input_basename

        # Pass 2: Build metadata from buffered headers (payloads already in zip)
        sorted_groups = sorted(
            (g for g in groups.values() if g.payload_filename),
            key=lambda g: g.response_order,
        )

        response_warc_rows = []
        response_warc_multi = []
        response_http_rows = []
        response_http_multi = []
        request_warc_rows = []
        request_warc_multi = []
        request_http_rows = []
        request_http_multi = []
        metadata_rows = []
        metadata_multi = []
        jsonl_entries = []

        for group in sorted_groups:
            if output_format == "sidecar":
                write_sidecar_files(outer_zip, root_dir, group)

            result = build_group_metadata(group, warc_filename, input_file)

            response_warc_rows.extend(result["response_warc_rows"])
            response_warc_multi.append(result["response_warc_multi"])
            response_http_rows.extend(result["response_http_rows"])
            response_http_multi.append(result["response_http_multi"])
            request_warc_rows.extend(result["request_warc_rows"])
            request_warc_multi.extend(result["request_warc_multi"])
            request_http_rows.extend(result["request_http_rows"])
            request_http_multi.extend(result["request_http_multi"])
            metadata_rows.extend(result["metadata_rows"])
            metadata_multi.extend(result["metadata_multi"])
            jsonl_entries.append(result["jsonl_entry"])

        # Write the warcinfo record(s): raw wire bytes plus greppable CSVs
        # Built unconditionally: warcinfo.csv records the source URI even for a WARC that
        # carries no warcinfo record, so the provenance is never lost.
        warcinfo_rows, warcinfo_multi = build_warcinfo_rows(warcinfos, input_file)
        if warcinfos:
            write_warcinfo_files(outer_zip, root_dir, warcinfos)

        # Write manifest.jsonl and its wide CSV mirror, both from the same entries
        jsonl_content = "\n".join(json.dumps(entry) for entry in jsonl_entries)
        outer_zip.writestr(f"{root_dir}/manifest.jsonl", jsonl_content)
        manifest_content, skipped = write_manifest_csv(jsonl_entries)
        outer_zip.writestr(f"{root_dir}/manifest.csv", manifest_content)

        # Write denormalized CSVs
        for name, rows in (
            ("response_warc_headers", response_warc_rows),
            ("response_http_headers", response_http_rows),
            ("request_warc_headers", request_warc_rows),
            ("request_http_headers", request_http_rows),
            ("metadata", metadata_rows),
            ("warcinfo", warcinfo_rows),
        ):
            content, n = write_denormalized_csv(rows, label=f"{name}.csv")
            outer_zip.writestr(f"{root_dir}/{name}.csv", content)
            skipped += n

        # Write multiline CSVs
        for name, rows in (
            ("response_warc_headers_multi", response_warc_multi),
            ("response_http_headers_multi", response_http_multi),
            ("request_warc_headers_multi", request_warc_multi),
            ("request_http_headers_multi", request_http_multi),
            ("metadata_multi", metadata_multi),
            ("warcinfo_multi", warcinfo_multi),
        ):
            content, n = write_multiline_csv(rows, label=f"{name}.csv")
            outer_zip.writestr(f"{root_dir}/{name}.csv", content)
            skipped += n

    print(
        f"Created {output_path}: {record_types['response']} responses, "
        f"{record_types['request']} requests, {record_types['metadata']} metadata records"
        + (" (metadata only, no payloads written)" if metadata_only else "")
    )
    print(format_record_type_summary(record_types))
    if skipped:
        print(f"warning: {skipped} CSV row(s) could not be written (see warnings above)", file=sys.stderr)

    return skipped


def cli():
    parser = argparse.ArgumentParser(description="Convert a gzipped WARC file into a zip-of-zips archive.")
    parser.add_argument("input_file", help="Path to a .warc.gz file, or '-' for stdin")
    parser.add_argument("--output", default=None, help="Output zip path (default: replace .warc.gz with .zip)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                        help=f"Skip processing, just print summary. Scans at most {DRY_RUN_MAX} capture records.")
    mode.add_argument(
        "--metadata-only",
        action="store_true",
        help="Write every CSV, manifest and sidecar but no payload files. The WARC is still "
        "streamed in full, so this saves output size, not transfer (use --limit for that).",
    )
    parser.add_argument(
        "--limit",
        help="Limit to N capture records, with their full set of associated request/metadata records",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--format",
        choices=["flat", "sidecar"],
        default="flat",
        help="Output format: 'flat' (counter-named files + global CSVs) or 'sidecar' (domain dirs + per-file metadata)",
    )
    args = parser.parse_args()

    input_file = args.input_file
    if args.output:
        output_path = Path(args.output)
    else:
        # Extract basename from local path or remote URI
        name = posixpath.basename(input_file.rstrip("/"))
        if name.endswith(".warc.gz"):
            name = name[: -len(".warc.gz")] + ".zip"
        else:
            name = name + ".zip"
        output_path = Path(name)

    skipped = main(
        input_file,
        str(output_path),
        dry_run=args.dry_run,
        limit=args.limit,
        output_format=args.format,
        metadata_only=args.metadata_only,
    )

    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(cli())
