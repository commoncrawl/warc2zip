# warc2zip

warc2zip converts WARC web archive files into zip archives, while preserving 100% of the metadata.

Each response record's payload is stored as a individual file with a proper extension, derived from its Content-Type.
Metadata from the request, response, and metadata records are  written to CSV (spreadsheet) files.

> [!WARNING]
> **Feedback is welcome**: this project is in early development. Feel free to open an issue or submit
> a pull request if you have suggestions, bug reports, or feature requests. See [WARC examples](#example-warc-for-testing) for testing below.

## Installation

> [!TIP]
> We recommend you to use a virtual environment (e.g. `uv` or `conda`) to avoid conflicts with other Python packages.

```bash
pip install .
```

By default, pip will install remote access tools, namely `fsspec` configured to talk to https and s3 remote files.

## Usage

```bash
warc2zip <path/to/file.warc.gz>
```

Input can be a local path or a remote URI (S3, HTTP, etc.):

```bash
warc2zip s3://commoncrawl/crawl-data/.../CC-MAIN-....warc.gz
warc2zip https://data.commoncrawl.org/crawl-data/.../CC-MAIN-....warc.gz
```

**Note**: `s3://commoncrawl` does **not** allow anonymous access — requests must be signed with credentials from any AWS account. To fetch Common Crawl data without an AWS account, use the HTTPS endpoint (`https://data.commoncrawl.org/...`) instead.

### Options

| Flag                      | Description                                                                            | Default                                 |
|---------------------------|----------------------------------------------------------------------------------------|-----------------------------------------|
| `input_file`              | Path or URI to a `.warc.gz` file (positional, required)                                |                                         |
| `--output`                | Path to the output zip file                                                            | Replace `.warc.gz` with `.zip`          |
| `--dry-run`               | Print summary without creating output. The scan always stops after at most 10 capture records, so it never streams the whole file; a lower `--limit` is respected |                                         |
| `--limit <N>`             | Limit to N capture records, with their full set of associated request/metadata records | No limit, all records are processed     |
| `--format {flat,sidecar}` | Output format (see [Output Formats](#output-formats) below)                            | `flat`                                  |

### Small Examples

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

Extract with sidecar format (per-file metadata, grouped by domain):

```bash
warc2zip archive.warc.gz --format sidecar --output result.zip
```

## Output Formats

All files are placed under a unique root directory inside the zip to prevent collisions when extracting multiple archives into the same folder. The directory name is derived from the WARC-Filename header (in the `warcinfo` record), the current timestamp, and a random suffix: `{crawl_name}_{YYYYMMDDTHHMMSS}_{hex}`.

This testing release of the software supports 2 output formats: flat and sidecar.
Flat puts the metadata into a small number of large files, and sidecar instead
creates a lot of metadata files, one for each payload.

### Flat format (`--format flat`, default)

Counter-named payload files with global CSV/JSONL metadata. Optimized for bulk analysis.

```
FOO.zip
  CC-MAIN-20251215005813-20251215035813-00995_20260330T143022_a1b2/
    1000000.html              # one per response record, extension from Content-Type
    1000001.pdf
    1000002.unk               # fallback for unknown mime-types
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

- **Payload files**: raw response bodies named `{counter}.{ext}`, where the extension is derived from the HTTP Content-Type header via `mimetypes.guess_extension()` (with overrides for common types like `text/html` → `.html`)
- **Denormalized CSVs** (`*_headers.csv`, `metadata.csv`): multiple rows per file — columns: `filename, header_name, header_value`
- **Multiline CSVs** (`*_multi.csv`): one row per file — columns: `filename, headers` (headers as a multiline string)

### Sidecar format (`--format sidecar`)

Captures grouped by domain (from `WARC-Target-URI`), with per-file metadata sidecars alongside each payload. Optimized for browsing individual captures. Global CSVs and manifest.jsonl are still generated.

```
FOO.zip
  CC-MAIN-20251215005813-20251215035813-00995_20260330T143022_a1b2/
    example.com/
      1000000.html                       # response payload
      1000000.html.request.warc          # request WARC headers
      1000000.html.request.http          # request HTTP headers (with request line)
      1000000.html.request.json          # request HTTP body (often empty for GET)
      1000000.html.response.warc         # response WARC headers
      1000000.html.response.http         # response HTTP headers (with status line)
      1000000.html.metadata.warc         # metadata WARC headers
      1000000.html.metadata.warc-fields  # metadata body (application/warc-fields)
    other.org/
      1000001.pdf
      1000001.pdf.response.warc
      1000001.pdf.response.http
      ...
    manifest.jsonl
    response_warc_headers.csv
    ...
```

- **Domain directories**: extracted from `WARC-Target-URI` (e.g. `https://example.com/page` → `example.com/`). Captures without a target URI go under `unknown/`.
- **Sidecar files**: named by appending a suffix to the payload filename. Headers are written in raw `Name: value` format (not normalized). Multiple metadata records for the same capture are merged, separated by blank lines.
- **Missing records are skipped**: if a capture has no request or metadata record, the corresponding sidecar files are simply not created.

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

## WARC examples for testing

We prepared some smaller (~1GBytes or less) and interesting WARC files for testing: US Federal government websites, homepages, etc.
These files are in a [Huggingface bucket](https://huggingface.co/buckets/commoncrawl/warc2zip-examples) and the `warc2zip` commands
below read directly from that bucket. These examples are `--format flat` ... you can also try `--format sidecar`

Details:

- Example WARC from CC-MAIN-2026-25 with 500 records (response, request and metadata, 13 MBytes) [CC-MAIN-2026-30-500_records.warc.gz](https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/CC-MAIN-2026-30-500_records.warc.gz?download=true)

  - make the zip
```
  warc2zip 'https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/CC-MAIN-2026-30-500_records.warc.gz?download=true' --format flat
```
  - here is the warcinfo

```
  isPartOf: CC-MAIN-2026-25
  publisher: Common Crawl
  description: Wide crawl of the web for June 2026
  operator: Common Crawl Admin (info@commoncrawl.org)
  hostname: ip-10-67-67-233
  software: Apache Nutch 1.21 (modified, https://github.com/commoncrawl/nutch/)
  robots: checked via crawler-commons 1.7-SNAPSHOT (https://github.com/crawler-commons/crawler-commons)
  format: WARC File Format 1.1
  conformsTo: https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/
  ```

- Homepages extracted from CC-MAIN-2026-21 (response records only, 1 GByte) [homepages_CC-MAIN-2026-21.warc.gz](https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/homepages_CC-MAIN-2026-21.warc.gz?download=true)
  - make the zip, note the limit
```
warc2zip 'https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/homepages_CC-MAIN-2026-21.warc.gz?download=true' --format flat --limit 1000
```
  - here is the warcinfo
```
  software: pypi_cdx_toolkit/0.9.40.dev89+g53a7ef76c
  isPartOf: CC-MAIN-2026-21
  description: Repackage of CC-MAIN-2026-21 containing only response records of homepages
  format: WARC file version 1.0
  creator: Common Crawl Foundation <https://commoncrawl.org>
  operator: Malte Ostendorff <mailto:malte@commoncrawl.org>
  ```
- URLs of federal institutions (response records only, 1/2 GByte), as part of the [End Of Term Archive](https://eotarchive.org/) project: [is_us_federal_CC-MAIN-2025-13.warc.gz](https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/is_us_federal_CC-MAIN-2025-13.warc.gz?download=true)
  - make the zip, note the limit
  ```
warc2zip 'https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/is_us_federal_CC-MAIN-2025-13.warc.gz?download=true' --format flat --limit 1000
  ```
  - here is the warcinfo
  ```
  software: pypi_cdx_toolkit/0.9.40.dev91+ga04800ea0
  isPartOf: CC-MAIN-2025-13
  description: Repackage of CC-MAIN-2025-13 containing only response records of US federal government hosts
  format: WARC file version 1.0
  creator: Common Crawl Foundation <https://commoncrawl.org>
  operator: Malte Ostendorff <mailto:malte@commoncrawl.org>
  ```
