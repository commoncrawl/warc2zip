import argparse
import csv
import io
import json
import mimetypes
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator


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
    requests: list[list[tuple[str, str]]] = field(default_factory=list)
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
    return mimetypes.guess_extension(mime_type) or ""


def write_denormalized_csv(rows):
    """Write denormalized CSV: filename, header_name, header_value (multiple rows per file)."""
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(["filename", "header_name", "header_value"])
    for row in rows:
        writer.writerow(row)
    return sio.getvalue()


def write_multiline_csv(rows):
    """Write multiline CSV: filename, headers (one row per file, headers as multiline string)."""
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(["filename", "headers"])
    for filename, header_pairs in rows:
        headers_str = "\n".join(f"{name}: {value}" for name, value in header_pairs)
        writer.writerow([filename, headers_str])
    return sio.getvalue()


def build_group_metadata(group):
    """Build CSV rows and JSONL entry for a RecordGroup whose payload is already in the zip."""
    payload_filename = group.payload_filename

    # Response WARC headers
    response_warc_rows = [(payload_filename, n, v) for n, v in group.response_warc_headers]
    response_warc_multi = (payload_filename, group.response_warc_headers)

    # Response HTTP headers
    response_http_rows = [(payload_filename, n, v) for n, v in group.response_http_headers]
    response_http_multi = (payload_filename, group.response_http_headers)

    # Request WARC headers (all requests use the response's payload_filename)
    request_warc_rows = []
    request_warc_multi_entries = []
    for req_pairs in group.requests:
        for n, v in req_pairs:
            request_warc_rows.append((payload_filename, n, v))
        request_warc_multi_entries.append((payload_filename, req_pairs))

    # Metadata entries (all use the response's payload_filename)
    metadata_rows = []
    metadata_multi_entries = []
    for meta_pairs, body_text in group.metadata_entries:
        for n, v in meta_pairs:
            metadata_rows.append((payload_filename, n, v))
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


def main(input_file, output_path, dry_run=False):
    file_size = Path(input_file).stat().st_size

    if dry_run:
        response_count = 0
        sample_uris = []
        sample_mimes = set()

        with open(input_file, "rb") as stream:
            with tqdm(total=file_size, unit="B", unit_scale=True, desc="Scanning") as pbar:
                for record in ArchiveIterator(stream):
                    record.content_stream().read()
                    if record.rec_type == "response":
                        response_count += 1
                        uri = record.rec_headers.get_header("WARC-Target-URI") or "unknown"
                        mime = detect_mime_type(record)
                        if len(sample_uris) < 5:
                            sample_uris.append(uri)
                        sample_mimes.add(mime)
                    pbar.update(stream.tell() - pbar.n)

        print(f"[dry-run] {response_count} response records found")
        print(f"[dry-run] Sample URIs: {sample_uris}")
        print(f"[dry-run] Detected mime-types: {sorted(sample_mimes)}")
        return

    groups = {}  # WARC-Record-ID -> RecordGroup
    order_counter = 0
    counter = 1_000_000

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as outer_zip:
        # Pass 1: Read WARC, write payloads immediately, buffer only headers
        with open(input_file, "rb") as stream:
            pbar = tqdm(total=file_size, unit="B", unit_scale=True, desc="Reading WARC")
            for record in ArchiveIterator(stream):
                rec_type = record.rec_type

                if rec_type == "response":
                    record_id = record.rec_headers.get_header("WARC-Record-ID")
                    target_uri = record.rec_headers.get_header("WARC-Target-URI")

                    # target URI maps to request record_id
                    group = groups.setdefault(target_uri, RecordGroup())
                    payload = record.content_stream().read()
                    group.response_mime_type = detect_mime_type(record)
                    ext = mime_to_extension(group.response_mime_type)
                    payload_filename = f"{counter}{ext}"
                    outer_zip.writestr(payload_filename, payload)
                    group.payload_filename = payload_filename
                    group.payload_size = len(payload)
                    group.response_record_id = record_id
                    group.response_target_uri = target_uri
                    group.response_date = record.rec_headers.get_header("WARC-Date") or ""
                    group.response_warc_headers = list(record.rec_headers.headers)
                    if record.http_headers:
                        group.response_http_headers = list(record.http_headers.headers)
                        group.content_type_header = record.http_headers.get_header("Content-Type") or ""
                        group.http_status_code = str(record.http_headers.get_statuscode() or "")
                    group.response_order = order_counter
                    order_counter += 1
                    counter += 1

                elif rec_type == "request":
                    record.content_stream().read()
                    target_uri = record.rec_headers.get_header("WARC-Target-URI")
                    if target_uri:
                        group = groups.setdefault(target_uri, RecordGroup())
                        group.requests.append(list(record.rec_headers.headers))

                elif rec_type == "metadata":
                    body = record.content_stream().read()
                    target_uri = record.rec_headers.get_header("WARC-Target-URI")
                    if target_uri:
                        group = groups.setdefault(target_uri, RecordGroup())
                        body_text = body.decode("utf-8", errors="replace")
                        group.metadata_entries.append((list(record.rec_headers.headers), body_text))

                else:
                    record.content_stream().read()

                pbar.update(stream.tell() - pbar.n)
            pbar.close()

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
        outer_zip.writestr("manifest.jsonl", jsonl_content)

        # Write denormalized CSVs
        outer_zip.writestr("response_warc_headers.csv", write_denormalized_csv(response_warc_rows))
        outer_zip.writestr("response_http_headers.csv", write_denormalized_csv(response_http_rows))
        outer_zip.writestr("request_warc_headers.csv", write_denormalized_csv(request_warc_rows))
        outer_zip.writestr("metadata.csv", write_denormalized_csv(metadata_rows))

        # Write multiline CSVs
        outer_zip.writestr("response_warc_headers_multi.csv", write_multiline_csv(response_warc_multi))
        outer_zip.writestr("response_http_headers_multi.csv", write_multiline_csv(response_http_multi))
        outer_zip.writestr("request_warc_headers_multi.csv", write_multiline_csv(request_warc_multi))
        outer_zip.writestr("metadata_multi.csv", write_multiline_csv(metadata_multi))

    print(f"Created {output_path} with {counter - 1_000_000} response records")


def cli():
    parser = argparse.ArgumentParser(description="Convert a gzipped WARC file into a zip-of-zips archive.")
    parser.add_argument("input_file", help="Path to a .warc.gz file")
    parser.add_argument("--output", default=None, help="Output zip path (default: replace .warc.gz with .zip)")
    parser.add_argument("--dry-run", action="store_true", help="Skip processing, just print summary")
    args = parser.parse_args()

    input_path = Path(args.input_file)
    if args.output:
        output_path = Path(args.output)
    else:
        name = input_path.name
        if name.endswith(".warc.gz"):
            name = name[: -len(".warc.gz")] + ".zip"
        else:
            name = name + ".zip"
        output_path = input_path.parent / name

    main(str(input_path), str(output_path), dry_run=args.dry_run)


if __name__ == "__main__":
    cli()
