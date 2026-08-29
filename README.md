# warc2zip

`warc2zip` converts WARC web archive files into zip archives, while preserving 100% of the metadata.

The goal of warc2zip is to facilitate low-code and no-code usage of web archives. A researcher
can make a selection of WARCs, perhaps from an Archive-It collection, perhaps a Browsertrix
collection, perhaps a repackage of Common Crawl containing just webpages labeled as being in
Swahili. `warc2zip` then converts these WARCs into zip files containing the web capture payloads,
with metadata stored as csv (spreadsheet) files.

FIXME: should we always write WARC (all caps) other than filenames?

Each response record's payload is stored as a individual file with a proper extension, derived from its Content-Type.
Metadata (both WARC and http) from the request, response, and metadata records are  written to CSV (spreadsheet) files.

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

**Note**: Please use s3 inside of AWS and https outside.

**Note**: `s3://commoncrawl` does **not** allow anonymous access — requests must be signed with credentials from any AWS account. To fetch Common Crawl data without an AWS account, use the HTTPS endpoint (`https://data.commoncrawl.org/...`) instead.

### Options

| Flag                      | Description                                                                            | Default                                 |
|---------------------------|----------------------------------------------------------------------------------------|-----------------------------------------|
| `input_file`              | Path or URI to a `.warc.gz` file (positional, required)                                |                                         |
| `--output`                | Path to the output zip file                                                            | Replace `.warc.gz` with `.zip`          |
| `--dry-run`               | Print summary without creating output. The scan always stops after at most 10 capture records, so it never streams the whole file; a lower `--limit` is respected |                                         |
| `--limit <N>`             | Limit to N capture records, with their full set of associated request/metadata records | No limit, all records are processed     |
| `--format {flat,sidecar}` | Output format (see [Output Formats](#output-formats) below)                            | `flat`                                  |
| `--metadata-only`         | Write every CSV, manifest and sidecar but no payload files                             | Off, payloads are written               |

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

Take the metadata and leave the payloads behind — for massaging the metadata before deciding which captures you actually want:

```bash
warc2zip archive.warc.gz --metadata-only --output metadata.zip
```

Every CSV, manifest, warcinfo file and sidecar is written exactly as it would be in a full run; only the payload files are omitted. Note this saves **output size, not transfer**: the WARC is still streamed from end to end, because counting and describing the records means reading them. `--limit` is the flag that shortens the read.

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
    warcinfo.warc             # the warcinfo record's WARC headers, raw
    warcinfo.warc-fields      # the warcinfo record's body, raw
    warcinfo.csv              # the same, parsed and greppable
    warcinfo_multi.csv
    manifest.jsonl            # one JSON line per response (mime-type, status, URI, etc.)
    manifest.csv              # the same, one wide row per response
    response_warc_headers.csv # filename, header_name, header_value
    response_warc_headers_multi.csv
    response_http_headers.csv
    response_http_headers_multi.csv
    request_warc_headers.csv
    request_warc_headers_multi.csv
    request_http_headers.csv
    request_http_headers_multi.csv
    metadata.csv
    metadata_multi.csv
```

- **Payload files**: raw response bodies named `{counter}.{ext}`, where the extension is derived from the HTTP Content-Type header via `mimetypes.guess_extension()` (with overrides for common types like `text/html` → `.html`)
- **Denormalized CSVs** (`*_headers.csv`, `metadata.csv`, `warcinfo.csv`): multiple rows per file — columns: `filename, header_name, header_value`. Header names are normalized to lowercase with `-` replaced by `_`.
- **Multiline CSVs** (`*_multi.csv`): one row per file — columns: `filename, headers` (headers as a multiline string)
- **`manifest.csv`**: the wide mirror of `manifest.jsonl` — one row per response, one column per key. Both are written from the same entries, so they cannot drift.
- **`warcinfo.*`**: crawl-level provenance (`isPartOf`, `publisher`, `software`, `hostname`, `conformsTo`, …). The `filename` column carries the synthetic key `warcinfo`, since the record belongs to no payload file; a concatenated WARC with several warcinfo records numbers the extras `warcinfo.1`, `warcinfo.2`, ….

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
    warcinfo.warc
    warcinfo.warc-fields
    manifest.jsonl
    manifest.csv
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
"1000000.html","content_length","34521"
"1000000.html","warc_protocol","h2"
"1000000.html","warc_protocol","tls/1.3"
```

Note the repeated `warc_protocol` rows: this is where the capture's real protocol lives. The HTTP status line in Common Crawl WARCs claims `HTTP/1.1` even for an h2 capture, which is why `status_code` below carries only the code and never the status line.

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

Each file's block opens with a synthetic `status_code` row, so a status can be grepped for directly:

```csv
"filename","header_name","header_value"
"1000000.html","status_code","200"
"1000000.html","content_type","text/html; charset=UTF-8"
"1000000.html","content_length","34521"
"1000000.html","server","nginx/1.18.0"
"1000000.html","date","Mon, 15 Dec 2025 00:58:13 GMT"
```

```bash
unzip -p FOO.zip '*/response_http_headers.csv' | grep '"status_code","403"'
```

`status_code` is a pseudo-header, not a header off the wire. A real response header literally named `Status-Code` would normalize to the same name — no such header exists in practice, but the collision is worth knowing about.

### request_warc_headers.csv (denormalized)

```csv
"filename","header_name","header_value"
"1000000.html","warc_type","request"
"1000000.html","warc_date","2025-12-15T00:58:13Z"
"1000000.html","warc_target_uri","https://example.com/page"
"1000000.html","warc_record_id","<urn:uuid:abcdef01-2345-...>"
```

### request_http_headers.csv (denormalized)

The request line is split into `request_method` / `request_target` pseudo-headers, the same way the status line becomes `status_code`:

```csv
"filename","header_name","header_value"
"1000000.html","request_method","GET"
"1000000.html","request_target","/"
"1000000.html","user_agent","CCBot/2.0 (https://commoncrawl.org/faq/)"
"1000000.html","accept_language","en-US,en;q=0.5"
"1000000.html","accept_encoding","zstd, br, gzip"
```

### metadata.csv (denormalized)

The metadata record's `application/warc-fields` body is shallow-flattened: one row per field, named `_body.<field>`. One level only — a field whose value is JSON (Common Crawl's `languages-cld2`) stays a single opaque value, because the list nested inside it does not flatten into columns usefully.

```csv
"filename","header_name","header_value"
"1000000.html","warc_type","metadata"
"1000000.html","warc_date","2025-12-15T00:58:13Z"
"1000000.html","warc_concurrent_to","<urn:uuid:12345678-abcd-...>"
"1000000.html","_body.fetchtimems","1044"
"1000000.html","_body.charset_detected","UTF-8"
"1000000.html","_body.languages_cld2","{""reliable"":false,""text-bytes"":29,""languages"":[{""code"":""zh"",""name"":""Chinese""}]}"
```

`metadata_multi.csv` keeps the body as one verbatim `_body` block instead, so the raw wire bytes survive in flat format too. A body that is not valid warc-fields falls back to a single raw `_body` row in both files.

### manifest.csv

The wide mirror of `manifest.jsonl` — one row per response, one column per key:

```csv
"filename","warc_record_id","warc_target_uri","warc_date","http_status_code","detected_mime_type","content_type_header","payload_size","warc_filename","source_uri","warc_record_offset","warc_record_length"
"1000000.html","<urn:uuid:12345678-abcd-...>","https://example.com/page","2025-12-15T00:58:13Z","200","text/html","text/html; charset=UTF-8","34521","CC-MAIN-20251215005813-20251215035813-00995.warc.gz","https://data.commoncrawl.org/crawl-data/CC-MAIN-2025-51/segments/.../CC-MAIN-...warc.gz","1062","3585"
```

The last four columns are what make a row re-fetchable on its own, and they are repeated on every row on purpose — no join against another file, no knowledge of the zip they came from:

| Column | Meaning |
|---|---|
| `warc_filename` | What the WARC **calls itself** (the warcinfo record's `WARC-Filename`), falling back to the input's basename. A label — for Common Crawl it is a bare basename, not a fetchable path. |
| `source_uri` | Where warc2zip **read the file from** — the `input_file` argument, verbatim. **This is the fetch target.** |
| `warc_record_offset` / `warc_record_length` | Byte range of the record. The same triple a CDX index carries. |

The offsets are positions in the file named by `source_uri` — that is the only file they are guaranteed to address. `warc_filename` may name a *different* file: a derived WARC (an extract, or the output of `tools/warc_limit.py`) copies the original warcinfo record, so it keeps advertising the original WARC's name while its byte offsets refer to the derived file. Use `source_uri` to fetch, `warc_filename` to say where the records originated. See [Building and downloading a subset](#building-and-downloading-a-subset).

### warcinfo.csv

Crawl-level provenance, with the record body flattened the same way as `metadata.csv`:

```csv
"filename","header_name","header_value"
"warcinfo","source_uri","https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-25/segments/.../CC-MAIN-...warc.gz"
"warcinfo","warc_record_offset","0"
"warcinfo","warc_record_length","519"
"warcinfo","warc_type","warcinfo"
"warcinfo","warc_filename","CC-MAIN-20260618163205-20260618193205-00999.warc.gz"
"warcinfo","_body.ispartof","CC-MAIN-2026-25"
"warcinfo","_body.publisher","Common Crawl"
"warcinfo","_body.software","Apache Nutch 1.21 (modified, https://github.com/commoncrawl/nutch/)"
"warcinfo","_body.conformsto","https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/"
```

`warcinfo.warc` and `warcinfo.warc-fields` hold the same record as raw wire bytes.

`source_uri` is the input this zip was converted from. It is also a column on every `manifest.csv` row; the copy here is the crawl-level one, written even when the WARC carries no warcinfo record at all, so the provenance is never lost.

Both `source_uri` and the `warc_record_*` fields are pseudo-headers — computed while streaming, not read off the wire. A real warcinfo header named `Source-URI` would normalize to the same name; none exists in practice, but the collision is worth knowing about, same as for `status_code`.

`warc_record_offset` and `warc_record_length` are not headers off the wire; they are computed while streaming, and appear for every record type — in `manifest.csv` for responses, and as rows in `response_warc_headers.csv`, `request_warc_headers.csv`, `metadata.csv` and `warcinfo.csv`.

## Building and downloading a subset

The point of `warc_filename` + `warc_record_offset` + `warc_record_length` is that `manifest.csv` alone is enough to fetch any capture again. Each record is its own gzip member, so those bytes are a standalone WARC — one HTTP range request per record, no reprocessing of the source file.

The intended workflow is: convert once with `--metadata-only` (a few tens of KB instead of gigabytes), filter the CSV however you like, then pull only the captures you kept.

```bash
# 1. metadata only — no payloads
warc2zip 'https://data.commoncrawl.org/crawl-data/.../CC-MAIN-....warc.gz' --metadata-only --output meta.zip
unzip -p meta.zip '*/manifest.csv' > manifest.csv
```

FIXME: replace 2. with grep and csv sorts of instructions

```
# 2. filter it with whatever you already use — here, everything that came back 200
python - <<'EOF'
import csv
with open("manifest.csv") as fh:
    rows = [r for r in csv.DictReader(fh) if r["http_status_code"] == "200"]
with open("subset.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows[0].keys(), quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
EOF
```

FIXME: replace 3. by expending warc2zip to do this download, with
appropriate retrying and rate limits. That might mean installing
cdx_toolkit and using it for this step.

```
# 3. fetch each surviving row — every row already knows where it came from
python - <<'EOF'
import csv, urllib.request
with open("subset.csv") as fh, open("subset.warc.gz", "wb") as out:
    for row in csv.DictReader(fh):
        start = int(row["warc_record_offset"])
        end = start + int(row["warc_record_length"]) - 1
        req = urllib.request.Request(row["source_uri"], headers={"Range": f"bytes={start}-{end}"})
        out.write(urllib.request.urlopen(req).read())
EOF
```

Concatenated gzip members are themselves a valid `.warc.gz`, so appending the fetched ranges into one file produces a WARC you can feed straight back into `warc2zip` — or into any other WARC tool.

Three caveats:

- Fetch with `source_uri`, not `warc_filename`. For Common Crawl the latter is a bare basename like `CC-MAIN-20260618163205-20260618193205-00999.warc.gz`; the full path is `crawl-data/{crawl}/segments/{segment}/warc/{basename}`, and **the segment is not recorded anywhere in the WARC** — `warcinfo.csv` gives you the crawl (`_body.ispartof`) but you would need the crawl's `warc.paths.gz` to resolve the rest.
- Offsets address the file named by `source_uri`, nothing else. A derived WARC keeps the original's warcinfo record, so its `warc_filename` names a file its offsets do not index.
- Offsets stay valid under `--limit`: limiting only stops the read early, it never rewrites them.

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

## Many more WARC examples for testing

### Common Crawl style repackaged warcs (intended for testing)

- https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/CC-MAIN-2026-30-500_records.warc.gz?download=true (13 MBytes)
- https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/homepages_CC-MAIN-2026-21.warc.gz?download=true (1 GByte)
- https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/is_us_federal_CC-MAIN-2025-13.warc.gz?download=true (1/2 GByte)

FIXME REPACKAGE should be in the name. Are these also on s3://commoncrawl/ ? projects/warc2zip-examples ?

### Normal Common Crawl CC-MAIN warcs

- prefixes: https://data.commoncrawl.org/ or s3://commoncrawl/
- crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/warc/CC-MAIN-20260807101845-20260807131845-00000.warc.gz
- crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/crawldiagnostics/CC-MAIN-20260807101845-20260807131845-00000.warc.gz
- crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/robotstxt/CC-MAIN-20260807101845-20260807131845-00000.warc.gz

FIXME: all 3 of these download to the same zip name

### End Of Term Archive (https://eotarchive.org/data/)

- prefixes: https://eotarchive.s3.amazonaws.com/ or s3://eotarchive/

#### Heretrix/IA style warcs from EOT 2024

- crawl-data/EOT-2024/segments/IA-000/EOT24PRE-20240926172119-crawl804_EOT24PRE-20240926172119-00000.warc.gz

#### Nutch/CCF style warcs from EOT 2024

- crawl-data/EOT-2024/segments/CC-000/warc/EOT-2024-REPACKAGE-CC-MAIN-2024-42-GOV-000000-001.warc.gz

#### Browsertrix style WARCs, EOT 2024

- crawl-data/EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-20250114215647018-0.warc.gz
- crawl-data/EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-screenshots-20250114215649547.warc.gz
- crawl-data/EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-text-20250114215649747.warc.gz

#### ArchiveTeam style megawarcs, EOT 2024 (warning: 10 gigabytes)

- crawl-data/EOT-2024/segments/AT-000/warc/archiveteam_usgovernment_20250131232111_96ad506d_usgovernment_20250131232111_96ad506d.1738361595.megawarc.warc.gz

#### Heretrix-style arcs from EOT 2004 (arc is the predecessor to warc)

- crawl-data/EOT-2004/segments/NARA-000/warc/NARA-PEOT-2004-20041014205819-00000-crawling009-c_NARA-PEOT-2004-20041014205819-00000-crawling009.archive.org.arc.gz

## Old CCF ARCs

- prefix: s3://commoncrawl/
- crawl-001/2008/06/19/0/1213886083018_0.arc.gz

## Cuil 2012

- prefix: not public
- domainshard-corpus5-large-merge-rev1.00004-of-25000.1000000sample.v1.arc.gz

## TODO

- CC-NEWS - old, pre-upgrade, post-upgrade
- ArchiveTeam warcs (not megawarcs)
- ArchiveIt old and new, for various flavors
