# warc2zip

Convert gzipped WARC files into zip archives. Each response record's payload is stored as a file with a proper extension (derived from Content-Type), alongside CSV and JSONL metadata.

> [!WARNING]
> **Work in progress**


## Installation

```bash
pip install .
```

For development:

```bash
pip install -e .
```

## Usage

```bash
warc2zip <path/to/file.warc.gz>
```

Input can be a local path or a remote URI (S3, HTTP, etc.):

```bash
warc2zip s3://commoncrawl/crawl-data/.../CC-MAIN-....warc.gz
warc2zip https://data.commoncrawl.org/crawl-data/.../CC-MAIN-....warc.gz
```

### Options

| Flag          | Description                                                                            | Default                                 |
|---------------|----------------------------------------------------------------------------------------|-----------------------------------------|
| `input_file`  | Path or URI to a `.warc.gz` file (positional, required)                                |                                         |
| `--output`    | Path to the output zip file                                                            | Replace `.warc.gz` with `.zip`          |
| `--dry-run`   | Print summary without creating output                                                  |                                         |
| `--limit <N>` | Limit to N capture records, with their full set of associated request/metadata records | No limit, all records are processed     |

### Examples

Preview records without writing anything:

```bash
warc2zip archive.warc.gz --dry-run
```

Specify output path:

```bash
warc2zip archive.warc.gz --output result.zip
```

Process a remote WARC file from S3:

```bash
warc2zip s3://bucket/path/archive.warc.gz --output local-result.zip
```

Sample the first 10 capture records (response + associated request/metadata) — useful for quickly inspecting a large remote WARC without streaming the whole file:

```bash
warc2zip s3://bucket/path/archive.warc.gz --limit 10 --output sample.zip
```

## Output Structure

All files are placed under a unique root directory inside the zip to prevent collisions when extracting multiple archives into the same folder. The directory name is derived from the WARC-Filename header (in the `warcinfo` record), the current timestamp, and a random suffix:

```
FOO.zip
  CC-MAIN-20251215005813-20251215035813-00995_20260330T143022_a1b2/
    1000000.html              # one per response record, extension from Content-Type
    1000001.pdf
    1000002                   # fallback for unknown mime-types
    ...
    manifest.jsonl            # one JSON line per response (mime-type, status, URI, etc.)
    response_warc_headers.csv # filename, header_name, header_value
    response_warc_headers_multi.csv
    response_http_headers.csv
    response_http_headers_multi.csv
    request_warc_headers.csv
    request_warc_headers_multi.csv
    metadata.csv
    metadata_multi.csv
```

- **Root directory**: `{crawl_name}_{YYYYMMDDTHHMMSS}_{hex}` — crawl name from `WARC-Filename`, UTC timestamp, 4-char random hex suffix. Sorting by name groups by crawl and orders by time.
- **Payload files**: raw response bodies named `{counter}.{ext}`, where the extension is derived from the HTTP Content-Type header via `mimetypes.guess_extension()` (with overrides for common types like `text/html` → `.html`)
- **Denormalized CSVs** (`*_headers.csv`, `metadata.csv`): multiple rows per file — columns: `filename, header_name, header_value`
- **Multiline CSVs** (`*_multi.csv`): one row per file — columns: `filename, headers` (headers as a multiline string)

## Metadata Examples

### manifest.jsonl

One JSON line per response record:

```json
{"filename": "1000000.html", "warc_record_id": "<urn:uuid:12345678-abcd-...>", "warc_target_uri": "https://example.com/page", "warc_date": "2025-12-15T00:58:13Z", "http_status_code": "200", "detected_mime_type": "text/html", "content_type_header": "text/html; charset=UTF-8", "payload_size": 34521}
{"filename": "1000001.pdf", "warc_record_id": "<urn:uuid:87654321-dcba-...>", "warc_target_uri": "https://example.com/doc.pdf", "warc_date": "2025-12-15T00:58:14Z", "http_status_code": "200", "detected_mime_type": "application/pdf", "content_type_header": "application/pdf", "payload_size": 102400}
```

### response_warc_headers.csv (denormalized)

One row per header per file:

```csv
"filename","header_name","header_value"
"1000000.html","warc_type","response"
"1000000.html","warc_date","2025-12-15T00:58:13Z"
"1000000.html","warc_target_uri","https://example.com/page"
"1000000.html","warc_record_id","<urn:uuid:12345678-abcd-...>"
"1000000.html","Content-Length","34521"
```

### response_warc_headers_multi.csv (multiline)

One row per file, all headers in a single multiline string. Header names are normalized to lowercase with `-` replaced by `_`:

```csv
"filename","headers"
"1000000.html","warc_type: response
warc_date: 2025-12-15T00:58:13Z
warc_target_uri: https://example.com/page
warc_record_id: <urn:uuid:12345678-abcd-...>
content_length: 34521"
```

### response_http_headers.csv (denormalized)

```csv
"filename","header_name","header_value"
"1000000.html","content_type","text/html; charset=UTF-8"
"1000000.html","content_length","34521"
"1000000.html","server","nginx/1.18.0"
"1000000.html","Date,"Mon, 15 Dec 2025 00:58:13 GMT"
```

### request_warc_headers.csv (denormalized)

```csv
"filename","header_name","header_value"
"1000000.html","warc_type","request"
"1000000.html","warc_date","2025-12-15T00:58:13Z"
"1000000.html","warc_target_uri","https://example.com/page"
"1000000.html","warc_record_id","<urn:uuid:abcdef01-2345-...>"
```

### metadata.csv (denormalized)

Includes a `_body` pseudo-header with the metadata record body:

```csv
"filename","header_name","header_value"
"1000000.html","warc_type","metadata"
"1000000.html","warc_date","2025-12-15T00:58:13Z"
"1000000.html","warc_concurrent_to","<urn:uuid:12345678-abcd-...>"
"1000000.html","_body,"fetchTimeMs: 245
charset-detected: UTF-8"
```
