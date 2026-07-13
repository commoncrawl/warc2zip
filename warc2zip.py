import argparse
import csv
import io
import json
import mimetypes
import posixpath
import secrets
import zipfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator
from warcio.utils import fsspec_open


MIME_EXTENSION_OVERRIDES = {
    "text/html": ".html",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
}


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
    http_status_code: str = ""
    content_type_header: str = ""
    response_order: int = -1
    http_status_line: str = ""  # Full HTTP status line, e.g. "HTTP/1.1 200 OK"
    response_domain: str = ""  # Domain from WARC-Target-URI, e.g. "example.com"
    concurrent_to: str = ""  # WARC-Concurrent-To from the response record (points to request)
    requests: list[list[tuple[str, str]]] = field(default_factory=list)
    request_http_headers: list[list[tuple[str, str]]] = field(default_factory=list)
    request_http_lines: list[str] = field(default_factory=list)  # e.g. "GET /path HTTP/1.1"
    request_bodies: list[bytes] = field(default_factory=list)
    metadata_entries: list[tuple[list[tuple[str, str]], str]] = field(default_factory=list)


def detect_mime_type(record):
    if record.http_headers:
        ct = record.http_headers.get_header("Content-Type")
        if ct:
            return ct.split(";")[0].strip().lower()
    return "application/octet-stream"


def mime_to_extension(mime_type):
    if mime_type in MIME_EXTENSION_OVERRIDES:
        return MIME_EXTENSION_OVERRIDES[mime_type]
    return mimetypes.guess_extension(mime_type) or ".unk"


def write_denormalized_csv(rows):
    """Write denormalized CSV: filename, header_name, header_value (multiple rows per file)."""
    sio = io.StringIO()
    writer = csv.writer(sio, quoting=csv.QUOTE_ALL)
    writer.writerow(["filename", "header_name", "header_value"])
    for row in rows:
        filename, header_name, header_value = row
        header_name = header_name.strip().lower().replace("-", "_")

        writer.writerow((filename, header_name, header_value))
    return sio.getvalue()


def write_multiline_csv(rows):
    """Write multiline CSV: filename, headers (one row per file, headers as multiline string)."""
    sio = io.StringIO()
    writer = csv.writer(sio, quoting=csv.QUOTE_ALL)
    writer.writerow(["filename", "headers"])
    for filename, header_pairs in rows:
        headers_str = "\n".join(
            f"{name.replace('-', '_').lower()}: {value}" for name, value in header_pairs
        )
        writer.writerow([filename, headers_str])
    return sio.getvalue()


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

    Strips path and extensions like .warc.gz to get a usable directory name.
    """
    name = posixpath.basename(warc_filename)
    if name.endswith(".warc.gz"):
        name = name[: -len(".warc.gz")]
    elif name.endswith(".warc"):
        name = name[: -len(".warc")]
    return name


def build_group_metadata(group):
    """Build CSV rows and JSONL entry for a RecordGroup whose payload is already in the zip."""
    payload_filename = group.payload_filename

    # Response WARC headers
    response_warc_rows = [(payload_filename, str.lower(n), v) for n, v in group.response_warc_headers]
    response_warc_multi = (payload_filename, group.response_warc_headers)

    # Response HTTP headers
    response_http_rows = [(payload_filename, str.lower(n), v) for n, v in group.response_http_headers]
    response_http_multi = (payload_filename, group.response_http_headers)

    # Request WARC headers (all requests use the response's payload_filename)
    request_warc_rows = []
    request_warc_multi_entries = []
    for req_pairs in group.requests:
        for n, v in req_pairs:
            request_warc_rows.append((payload_filename, str.lower(n.replace("-", "_")), v))
        request_warc_multi_entries.append((payload_filename, req_pairs))

    # Metadata entries (all use the response's payload_filename)
    metadata_rows = []
    metadata_multi_entries = []
    for meta_pairs, body_text in group.metadata_entries:
        for n, v in meta_pairs:
            metadata_rows.append((payload_filename, str.lower(n.replace("-", "_")), v))
        metadata_rows.append((payload_filename, "_body", body_text))
        all_pairs = meta_pairs + [("_body", body_text)]
        metadata_multi_entries.append((payload_filename, all_pairs))

    # JSONL manifest entry
    jsonl_entry = {
        "filename": payload_filename,
        "warc_record_id": group.response_record_id,
        "warc_target_uri": group.response_target_uri,
        "warc_date": group.response_date,
        "http_status_code": group.http_status_code,
        "detected_mime_type": group.response_mime_type or "application/octet-stream",
        "content_type_header": group.content_type_header,
        "payload_size": group.payload_size,
    }

    return {
        "response_warc_rows": response_warc_rows,
        "response_warc_multi": response_warc_multi,
        "response_http_rows": response_http_rows,
        "response_http_multi": response_http_multi,
        "request_warc_rows": request_warc_rows,
        "request_warc_multi": request_warc_multi_entries,
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
        for meta_headers, body_text in group.metadata_entries:
            warc_parts.append("\n".join(f"{n}: {v}" for n, v in meta_headers))
            body_parts.append(body_text)
        zip_file.writestr(f"{base}.metadata.warc", "\n\n".join(warc_parts))
        zip_file.writestr(f"{base}.metadata.warc-fields", "\n\n".join(body_parts))


def main(input_file, output_path, dry_run=False, limit=None, output_format="flat"):
    file_size = get_file_size(input_file)

    if dry_run:
        response_count = 0
        request_count = 0
        metadata_count = 0
        sample_uris = []
        sample_mimes = set()
        limit_reached = False

        with fsspec_open(input_file, "rb") as stream:
            with tqdm(total=file_size, unit="B", unit_scale=True, desc="Scanning") as pbar:
                for record in ArchiveIterator(stream):
                    if limit_reached and record.rec_type == "response":
                        break
                    record.content_stream().read()
                    if record.rec_type == "response":
                        response_count += 1
                        uri = record.rec_headers.get_header("WARC-Target-URI") or "unknown"
                        mime = detect_mime_type(record)
                        if len(sample_uris) < 5:
                            sample_uris.append(uri)
                        sample_mimes.add(mime)
                        if limit is not None and response_count >= limit:
                            limit_reached = True
                    elif record.rec_type == "request":
                        request_count += 1
                    elif record.rec_type == "metadata":
                        metadata_count += 1
                    pbar.update(stream.tell() - pbar.n)

        print(f"[dry-run] {response_count} responses, {request_count} requests, {metadata_count} metadata records")
        print(f"[dry-run] Sample URIs: {sample_uris}")
        print(f"[dry-run] Detected mime-types: {sorted(sample_mimes)}")
        return

    groups = {}  # response WARC-Record-ID -> RecordGroup
    pending_requests = {}  # request WARC-Record-ID -> list of header tuples
    order_counter = 0
    counter = 1_000_000
    root_dir = None  # resolved from first warcinfo record

    # Fallback crawl name from input filename
    input_basename = posixpath.basename(input_file.rstrip("/"))
    fallback_crawl_name = extract_crawl_name(input_basename) if input_basename else "unknown"

    response_count = 0
    request_count = 0
    metadata_count = 0
    limit_reached = False

    # response -> Record id  <-> metadata -
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as outer_zip:
        # Pass 1: Read WARC, write payloads immediately, buffer only headers
        with fsspec_open(input_file, "rb") as stream:
            pbar = tqdm(total=file_size, unit="B", unit_scale=True, desc="Reading WARC")
            for record in ArchiveIterator(stream):
                rec_type = record.rec_type

                if limit_reached and rec_type == "response":
                    break

                if rec_type == "warcinfo":
                    record.content_stream().read()
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

                    group = groups.setdefault(record_id, RecordGroup())
                    group.concurrent_to = record.rec_headers.get_header("WARC-Concurrent-To") or ""
                    payload = record.content_stream().read()
                    group.response_mime_type = detect_mime_type(record)
                    ext = mime_to_extension(group.response_mime_type)
                    payload_filename = f"{counter}{ext}"

                    # Extract domain for sidecar format directory grouping
                    domain = urlparse(target_uri).netloc if target_uri else "unknown"
                    group.response_domain = domain or "unknown"

                    if output_format == "sidecar":
                        zip_path = f"{root_dir}/{group.response_domain}/{payload_filename}"
                    else:
                        zip_path = f"{root_dir}/{payload_filename}"
                    outer_zip.writestr(zip_path, payload)

                    group.payload_filename = payload_filename
                    group.payload_size = len(payload)
                    group.response_record_id = record_id
                    group.response_target_uri = target_uri or ""
                    group.response_date = record.rec_headers.get_header("WARC-Date") or ""
                    group.response_warc_headers = list(record.rec_headers.headers)
                    if record.http_headers:
                        group.response_http_headers = list(record.http_headers.headers)
                        group.content_type_header = record.http_headers.get_header("Content-Type") or ""
                        group.http_status_code = str(record.http_headers.get_statuscode() or "")
                        group.http_status_line = f"{record.http_headers.protocol} {record.http_headers.statusline}"
                    group.response_order = order_counter
                    order_counter += 1
                    counter += 1
                    response_count += 1
                    if limit is not None and response_count >= limit:
                        limit_reached = True

                elif rec_type == "request":
                    request_count += 1
                    body = record.content_stream().read()
                    request_record_id = record.rec_headers.get_header("WARC-Record-ID")
                    req_entry = {
                        "warc_headers": list(record.rec_headers.headers),
                        "http_headers": list(record.http_headers.headers) if record.http_headers else [],
                        "http_request_line": f"{record.http_headers.protocol} {record.http_headers.statusline}" if record.http_headers else "",
                        "body": body,
                    }
                    pending_requests.setdefault(request_record_id, []).append(req_entry)

                elif rec_type == "metadata":
                    metadata_count += 1
                    body = record.content_stream().read()
                    concurrent_to = record.rec_headers.get_header("WARC-Concurrent-To")

                    group = groups.setdefault(concurrent_to, RecordGroup())
                    body_text = body.decode("utf-8", errors="replace")
                    group.metadata_entries.append((list(record.rec_headers.headers), body_text))

                else:
                    record.content_stream().read()

                pbar.update(stream.tell() - pbar.n)
            pbar.close()

        # Link pending requests to their response groups via WARC-Concurrent-To
        for group in groups.values():
            if group.concurrent_to and group.concurrent_to in pending_requests:
                for req in pending_requests[group.concurrent_to]:
                    group.requests.append(req["warc_headers"])
                    group.request_http_headers.append(req["http_headers"])
                    group.request_http_lines.append(req["http_request_line"])
                    group.request_bodies.append(req["body"])

        # Warn about orphan records (request/metadata without a matching response)
        orphan_count = sum(1 for g in groups.values() if not g.payload_filename)
        if orphan_count:
            print(f"Warning: {orphan_count} orphan group(s) without a response record, skipped")

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
        metadata_rows = []
        metadata_multi = []
        jsonl_entries = []

        for group in sorted_groups:
            if output_format == "sidecar":
                write_sidecar_files(outer_zip, root_dir, group)

            result = build_group_metadata(group)

            response_warc_rows.extend(result["response_warc_rows"])
            response_warc_multi.append(result["response_warc_multi"])
            response_http_rows.extend(result["response_http_rows"])
            response_http_multi.append(result["response_http_multi"])
            request_warc_rows.extend(result["request_warc_rows"])
            request_warc_multi.extend(result["request_warc_multi"])
            metadata_rows.extend(result["metadata_rows"])
            metadata_multi.extend(result["metadata_multi"])
            jsonl_entries.append(result["jsonl_entry"])

        # Write manifest.jsonl
        jsonl_content = "\n".join(json.dumps(entry) for entry in jsonl_entries)
        outer_zip.writestr(f"{root_dir}/manifest.jsonl", jsonl_content)

        # Write denormalized CSVs
        outer_zip.writestr(f"{root_dir}/response_warc_headers.csv", write_denormalized_csv(response_warc_rows))
        outer_zip.writestr(f"{root_dir}/response_http_headers.csv", write_denormalized_csv(response_http_rows))
        outer_zip.writestr(f"{root_dir}/request_warc_headers.csv", write_denormalized_csv(request_warc_rows))
        outer_zip.writestr(f"{root_dir}/metadata.csv", write_denormalized_csv(metadata_rows))

        # Write multiline CSVs
        outer_zip.writestr(f"{root_dir}/response_warc_headers_multi.csv", write_multiline_csv(response_warc_multi))
        outer_zip.writestr(f"{root_dir}/response_http_headers_multi.csv", write_multiline_csv(response_http_multi))
        outer_zip.writestr(f"{root_dir}/request_warc_headers_multi.csv", write_multiline_csv(request_warc_multi))
        outer_zip.writestr(f"{root_dir}/metadata_multi.csv", write_multiline_csv(metadata_multi))

    print(
        f"Created {output_path}: {response_count} responses, "
        f"{request_count} requests, {metadata_count} metadata records"
    )


def cli():
    parser = argparse.ArgumentParser(description="Convert a gzipped WARC file into a zip-of-zips archive.")
    parser.add_argument("input_file", help="Path to a .warc.gz file")
    parser.add_argument("--output", default=None, help="Output zip path (default: replace .warc.gz with .zip)")
    parser.add_argument("--dry-run", action="store_true", help="Skip processing, just print summary")
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

    main(input_file, str(output_path), dry_run=args.dry_run, limit=args.limit, output_format=args.format)


if __name__ == "__main__":
    cli()
