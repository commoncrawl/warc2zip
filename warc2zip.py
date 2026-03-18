import argparse
import csv
import io
import json
import mimetypes
import zipfile
from pathlib import Path

from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator


MIME_EXTENSION_OVERRIDES = {
    'text/html': '.html',
    'text/plain': '.txt',
    'image/jpeg': '.jpg',
}


def detect_mime_type(record):
    if record.http_headers:
        ct = record.http_headers.get_header('Content-Type')
        if ct:
            return ct.split(';')[0].strip().lower()
    return 'application/octet-stream'


def mime_to_extension(mime_type):
    if mime_type in MIME_EXTENSION_OVERRIDES:
        return MIME_EXTENSION_OVERRIDES[mime_type]
    return mimetypes.guess_extension(mime_type) or '.bin'


def write_denormalized_csv(rows):
    """Write denormalized CSV: filename, header_name, header_value (multiple rows per file)."""
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(['filename', 'header_name', 'header_value'])
    for row in rows:
        writer.writerow(row)
    return sio.getvalue()


def write_multiline_csv(rows):
    """Write multiline CSV: filename, headers (one row per file, headers as multiline string)."""
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(['filename', 'headers'])
    for filename, header_pairs in rows:
        headers_str = '\n'.join(f'{name}: {value}' for name, value in header_pairs)
        writer.writerow([filename, headers_str])
    return sio.getvalue()


    counter = 1_000_000
    response_index = {}  # WARC-Record-ID -> filename (without .zip)

    response_warc_rows = []       # denormalized: (filename, name, value)
    response_warc_multi = []      # multiline: (filename, [(name, value), ...])
    response_http_rows = []
    response_http_multi = []
    request_warc_rows = []
    request_warc_multi = []
    metadata_rows = []
    metadata_multi = []
    jsonl_entries = []

def main(input_file, output_path, dry_run=False):
    file_size = Path(input_file).stat().st_size

    if dry_run:
        response_count = 0
        sample_uris = []
        sample_mimes = set()

        with open(input_file, 'rb') as stream:
            with tqdm(total=file_size, unit='B', unit_scale=True, desc='Scanning') as pbar:
                for record in ArchiveIterator(stream):
                    record.content_stream().read()
                    if record.rec_type == 'response':
                        response_count += 1
                        uri = record.rec_headers.get_header('WARC-Target-URI') or 'unknown'
                        mime = detect_mime_type(record)
                        if len(sample_uris) < 5:
                            sample_uris.append(uri)
                        sample_mimes.add(mime)
                    pbar.update(stream.tell() - pbar.n)

        print(f"[dry-run] {response_count} response records found")
        print(f"[dry-run] Sample URIs: {sample_uris}")
        print(f"[dry-run] Detected mime-types: {sorted(sample_mimes)}")
        return

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as outer_zip:
        with open(input_file, 'rb') as stream:
            pbar = tqdm(total=file_size, unit='B', unit_scale=True, desc='Converting')
            for record in ArchiveIterator(stream):
                rec_type = record.rec_type

                if rec_type == 'response':
                    payload = record.content_stream().read()
                    filename = str(counter)
                    record_id = record.rec_headers.get_header('WARC-Record-ID')
                    response_index[record_id] = filename

                    mime = detect_mime_type(record)
                    ext = mime_to_extension(mime)

                    outer_zip.writestr(f'{filename}{ext}', payload)

                    # WARC headers
                    warc_pairs = []
                    for name, value in record.rec_headers.headers:
                        response_warc_rows.append((filename, name, value))
                        warc_pairs.append((name, value))
                    response_warc_multi.append((filename, warc_pairs))

                    # HTTP headers
                    http_pairs = []
                    if record.http_headers:
                        for name, value in record.http_headers.headers:
                            response_http_rows.append((filename, name, value))
                            http_pairs.append((name, value))
                    response_http_multi.append((filename, http_pairs))

                    # Manifest entry
                    ct_header = ''
                    if record.http_headers:
                        ct_header = record.http_headers.get_header('Content-Type') or ''
                    http_status = ''
                    if record.http_headers:
                        http_status = str(record.http_headers.get_statuscode() or '')
                    jsonl_entries.append({
                        'filename': filename,
                        'warc_record_id': record_id,
                        'warc_target_uri': record.rec_headers.get_header('WARC-Target-URI') or '',
                        'warc_date': record.rec_headers.get_header('WARC-Date') or '',
                        'http_status_code': http_status,
                        'detected_mime_type': mime,
                        'content_type_header': ct_header,
                        'payload_size': len(payload),
                    })

                    counter += 1

                elif rec_type == 'request':
                    record.content_stream().read()
                    concurrent_to = record.rec_headers.get_header('WARC-Concurrent-To')
                    filename = response_index.get(concurrent_to, '')

                    warc_pairs = []
                    for name, value in record.rec_headers.headers:
                        request_warc_rows.append((filename, name, value))
                        warc_pairs.append((name, value))
                    request_warc_multi.append((filename, warc_pairs))

                elif rec_type == 'metadata':
                    body = record.content_stream().read()
                    concurrent_to = record.rec_headers.get_header('WARC-Concurrent-To')
                    filename = response_index.get(concurrent_to, '')

                    warc_pairs = []
                    for name, value in record.rec_headers.headers:
                        metadata_rows.append((filename, name, value))
                        warc_pairs.append((name, value))
                    # Add body as a special row
                    body_text = body.decode('utf-8', errors='replace')
                    metadata_rows.append((filename, '_body', body_text))
                    warc_pairs.append(('_body', body_text))
                    metadata_multi.append((filename, warc_pairs))

                else:
                    # warcinfo and others: consume stream, skip
                    record.content_stream().read()

                pbar.update(stream.tell() - pbar.n)
            pbar.close()

        # Write manifest.jsonl
        jsonl_content = '\n'.join(json.dumps(entry) for entry in jsonl_entries)
        outer_zip.writestr('manifest.jsonl', jsonl_content)

        # Write denormalized CSVs
        outer_zip.writestr('response_warc_headers.csv', write_denormalized_csv(response_warc_rows))
        outer_zip.writestr('response_http_headers.csv', write_denormalized_csv(response_http_rows))
        outer_zip.writestr('request_warc_headers.csv', write_denormalized_csv(request_warc_rows))
        outer_zip.writestr('metadata.csv', write_denormalized_csv(metadata_rows))

        # Write multiline CSVs
        outer_zip.writestr('response_warc_headers_multi.csv', write_multiline_csv(response_warc_multi))
        outer_zip.writestr('response_http_headers_multi.csv', write_multiline_csv(response_http_multi))
        outer_zip.writestr('request_warc_headers_multi.csv', write_multiline_csv(request_warc_multi))
        outer_zip.writestr('metadata_multi.csv', write_multiline_csv(metadata_multi))

    print(f"Created {output_path} with {counter - 1_000_000} response records")


def cli():
    parser = argparse.ArgumentParser(description='Convert a gzipped WARC file into a zip-of-zips archive.')
    parser.add_argument('input_file', help='Path to a .warc.gz file')
    parser.add_argument('--output', default=None, help='Output zip path (default: replace .warc.gz with .zip)')
    parser.add_argument('--dry-run', action='store_true', help='Skip processing, just print summary')
    args = parser.parse_args()

    input_path = Path(args.input_file)
    if args.output:
        output_path = Path(args.output)
    else:
        name = input_path.name
        if name.endswith('.warc.gz'):
            name = name[:-len('.warc.gz')] + '.zip'
        else:
            name = name + '.zip'
        output_path = input_path.parent / name

    main(str(input_path), str(output_path), dry_run=args.dry_run)


if __name__ == '__main__':
    cli()
