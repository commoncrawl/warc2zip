import argparse
import os
import sys
import time
import urllib.request
from pathlib import Path

import cdx_toolkit
import duckdb
import pandas as pd
from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator


def get_urls(input_columnar_index, query):
    retries_left = 100

    while True:
        try:
            ccindex = duckdb.read_parquet(input_columnar_index, hive_partitioning=True)
            break
        except (duckdb.HTTPException, duckdb.InvalidInputException) as e:
            # read_parquet exception seen: HTTPException("HTTP Error: HTTP GET error on 'https://...' (HTTP 403)")
            # duckdb.duckdb.InvalidInputException: Invalid Input Error: No magic bytes found at end of file 'https://...'
            print("read_parquet exception seen:", repr(e), file=sys.stderr)
            if retries_left:
                print("sleeping for 60s", file=sys.stderr)
                time.sleep(60)
                retries_left -= 1
            else:
                raise

    duckdb.sql("SET enable_progress_bar = true;")
    duckdb.sql("SET http_retries = 100;")

    rows = duckdb.sql(query)

    return rows


def get_crawls_coordinates(input_host_index, url_rows, homepage=False):
    ## Get offset, lenght and filename of each url in the host index

    files = [str(f) for f in Path(os.path.expanduser(f"{input_host_index}")).rglob("*.parquet")]

    retries_left = 100

    while True:
        try:
            ccindex = duckdb.read_parquet(files, hive_partitioning=True)
            break
        except (duckdb.HTTPException, duckdb.InvalidInputException) as e:
            # read_parquet exception seen: HTTPException("HTTP Error: HTTP GET error on 'https://...' (HTTP 403)")
            # duckdb.duckdb.InvalidInputException: Invalid Input Error: No magic bytes found at end of file 'https://...'
            print("read_parquet exception seen:", repr(e), file=sys.stderr)
            if retries_left:
                print("sleeping for 60s", file=sys.stderr)
                time.sleep(60)
                retries_left -= 1
            else:
                raise

    duckdb.sql("SET enable_progress_bar = true;")
    duckdb.sql("SET http_retries = 100;")

    crawl_coordinates = []

    df = url_rows.fetchdf()
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Looking up coordinates"):
        host = row["url_host_name"]
        tld = row["url_host_tld"]
        domain = row["url_host_registered_domain"]

        # Filter out DNS pseudo-URLs (`dns:hostname`) that share the same host columns
        # as real HTTP captures. When `homepage=True`, also restrict to the root path.
        homepage_clause = "AND url_path = '/'" if homepage else ""

        eot_http_clause = "AND url LIKE 'http%'"

        sq2 = f"""
            SELECT
              url, url_host_name, warc_filename, warc_record_offset, warc_record_length, crawl, warc_segment
            FROM ccindex
            WHERE subset = 'warc'
              AND url_host_tld = '{tld}' -- help the query optimizer
              AND url_host_registered_domain = '{domain}' -- ditto
              AND url_host_name = '{host}'              
              {homepage_clause}
            ;
            """

        # sq2 = f"""
        #     SELECT
        #       url, url_host_name, warc_filename, warc_record_offset, warc_record_length, crawl, warc_segment
        #     FROM ccindex
        #     WHERE subset = 'warc'
        #       AND url_host_tld = '{tld}' -- help the query optimizer
        #       AND url_host_registered_domain = '{domain}' -- ditto
        #       AND url_host_name = '{host}'
        #       ;
        #     """

        result = duckdb.sql(sq2).fetchdf()
        if not result.empty:
            # result.insert(0, "url_host_name", host)
            crawl_coordinates.append(result)

    return crawl_coordinates


def fetch_warc_records(crawl_coordinates, prefix="https://data.commoncrawl.org"):
    """Fetch WARC records by streaming each source WARC file once and URL-matching."""
    warcinfo = {
        "software": "build_custom_warc",
        "isPartOf": "WARC2ZIP-COMMONCRAWL",
        "description": "warc extraction",
        "format": "WARC file version 1.1",
    }
    writer = cdx_toolkit.warc.get_writer("WARC2ZIP", "COMMONCRAWL", warcinfo, warc_version="1.0")

    if not crawl_coordinates:
        return 0

    all_coords = pd.concat(crawl_coordinates, ignore_index=True)
    all_coords = all_coords.sort_values(["warc_filename", "warc_record_offset"])
    grouped = all_coords.groupby("warc_filename", sort=False)

    nb_written_captures = 0
    prefix = prefix.rstrip("/")

    for filename, group in tqdm(grouped, desc="Fetching WARC files"):
        remaining = set(group["url"].tolist())
        url = f"{prefix}/{filename}"

        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                for record in ArchiveIterator(resp):
                    if not remaining:
                        break
                    rec_url = record.rec_headers.get_header("WARC-Target-URI")
                    if rec_url in remaining:
                        writer.write_record(record)
                        remaining.discard(rec_url)
                        nb_written_captures += 1
        except Exception as e:
            print(f"  Failed on {filename}: {e}", file=sys.stderr)

        if remaining:
            print(f"  {len(remaining)} URL(s) not found in {filename}", file=sys.stderr)

    return nb_written_captures


def main(host_index, columnar_index, limit=None, homepage=False, warc_prefix="https://data.commoncrawl.org"):
    limit_clause = f"LIMIT {limit}" if limit else ""
    query_is_us_federal = f"""
         SELECT DISTINCT url_host_name, url_host_tld, url_host_registered_domain
         FROM ccindex
         WHERE is_us_federal = True
         {limit_clause};
         """

    rows_urls = get_urls(host_index, query_is_us_federal)

    print(f"Fetched {len(rows_urls)} urls")
    crawl_coordinates = get_crawls_coordinates(columnar_index, rows_urls, homepage=homepage)

    print(f"Collected {len(crawl_coordinates)} captures.")

    nb_written_captures = fetch_warc_records(crawl_coordinates, prefix=warc_prefix)

    print(f"Wrote {nb_written_captures}/{len(crawl_coordinates)} captures from {len(rows_urls)} urls.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch WARC records from Common Crawl using host/columnar indexes")
    parser.add_argument(
        "--columnar-index",
        type=str,
        metavar="DIRECTORY",
        help="Path to directory containing the columnar index parquet files",
        required=True,
    )
    parser.add_argument(
        "--host-index",
        type=str,
        metavar="FILE",
        help="Path to host-level index parquet file",
        required=True
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum number of hosts to process"
    )
    parser.add_argument(
        "--homepage", action="store_true", help="Fetch only the homepage (url_path = '/') of each host"
    )
    parser.add_argument(
        "--warc-prefix",
        type=str,
        default="https://data.commoncrawl.org",
        help="Base URL prefix for WARC files (default: %(default)s)",
    )
    args = parser.parse_args()

    main(
        args.host_index,
        args.columnar_index,
        limit=args.limit,
        homepage=args.homepage,
        warc_prefix=args.warc_prefix,
    )
