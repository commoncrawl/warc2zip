# build_custom_warc

Build custom WARC files from Common Crawl indexes.

## Pipeline

```mermaid
flowchart TD
    Q["SQL Query\n(e.g. is_us_federal = True)"]
    CI["Host Index\n(parquet)"]
    U["URLs\n(host_name, tld,\nregistered_domain)"]
    HI["Columnar Index\n(parquet directory)"]
    WC["WARC Coordinates\n(filename, offset,\nlength, surt)"]
    CDX["CDX Server Fetch\n(cdx_toolkit)"]
    W["WARC File\n(output)"]

    Q --> CI --> U --> HI --> WC --> CDX --> W
```

## Usage

```bash
python build_custom_warc.py \
    --columnar-index "s3://commoncrawl/cc-index/table/cc-main/warc/*.parquet" \
    --host-index ~/cc-host-index/ \
    --limit 10
```
