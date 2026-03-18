# warc2zip

Convert gzipped WARC files into zip archives. Each response record's payload is stored as a file with a proper extension (derived from Content-Type), alongside CSV and JSONL metadata.

## Requirements

- Python 3.8+
- [warcio](https://github.com/webrecorder/warcio)
- [tqdm](https://github.com/tqdm/tqdm)

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

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `input_file` | Path to a `.warc.gz` file (positional, required) | |
| `--output` | Path to the output zip file | Replace `.warc.gz` with `.zip` |
| `--threads` | Number of worker threads | 4 |
| `--dry-run` | Print summary without creating output | |

### Examples

Preview records without writing anything:

```bash
warc2zip archive.warc.gz --dry-run
```

Specify output path:

```bash
warc2zip archive.warc.gz --output result.zip
```

## Output Structure

```
FOO.zip
  1000000.html              # one per response record, extension from Content-Type
  1000001.pdf
  1000002.bin               # fallback for unknown mime-types
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
