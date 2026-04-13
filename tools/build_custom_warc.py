import argparse
import os
import sys
import time
from pathlib import Path

import cdx_toolkit
import duckdb
from tqdm import tqdm


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


def fetch_warc_records(crawl_coordinates):
    """Fetch WARC records from Common Crawl by coordinates and write to a WARC file."""
    warcinfo = {
        "software": "build_custom_warc",
        "isPartOf": "WARC2ZIP-COMMONCRAWL",
        "description": "warc extraction",
        "format": "WARC file version 1.1",
    }
    writer = cdx_toolkit.warc.get_writer("WARC2ZIP", "COMMONCRAWL", warcinfo, warc_version="1.0")

    for coord_df in tqdm(crawl_coordinates, desc="Fetching WARC records"):
        for _, row in coord_df.iterrows():
            capture = {
                "url": row["url"],
                "filename": row["warc_filename"],
                "offset": int(row["warc_record_offset"]),
                "length": int(row["warc_record_length"]),
            }

            try:
                # record = cdx_toolkit.warc.fetch_warc_record(capture, "https://eotarchive.s3.amazonaws.com/")
                record = cdx_toolkit.warc.fetch_warc_record(capture, "https://data.commoncrawl.org/")
                writer.write_record(record)
                print(f"  Wrote record from {row['url']}")
            except Exception as e:
                print(f"  Failed to fetch {row['url']}: {e}", file=sys.stderr)

    print(f"Wrote {len(crawl_coordinates)} records")


def main(host_index, columnar_index, limit=None, homepage=False):
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

    fetch_warc_records(crawl_coordinates)


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
    args = parser.parse_args()

    main(args.host_index, args.columnar_index, limit=args.limit, homepage=args.homepage)
