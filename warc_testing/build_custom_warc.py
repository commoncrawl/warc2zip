import argparse
import os
import sys
import time
from pathlib import Path

import duckdb


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

    # sq2 = f'''
    # select url_host_name
    # from ccindex
    # where is_us_federal = True;
    # '''
    #
    # rows_is_us_federal = duckdb.sql(sq2)
    # df = rows_is_us_federal.fetchdf()

    rows = duckdb.sql(query)

    return rows


def get_crawls_coordinates(input_host_index, url_rows):
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
    for _, row in df.iterrows():
        host = row["url_host_name"]
        tld = row["url_host_tld"]
        domain = row["url_host_registered_domain"]

        sq2 = f"""
            SELECT
              url, warc_filename, warc_record_offset, warc_record_length
            FROM ccindex
            WHERE subset = 'warc'
              AND url_host_tld = '{tld}' -- help the query optimizer
              AND url_host_registered_domain = '{domain}' -- ditto
              AND url_host_name = '{host}'
            LIMIT 1;
            """

        result = duckdb.sql(sq2).fetchdf()
        if not result.empty:
            result.insert(0, "url_host_name", host)
            crawl_coordinates.append(result)
            print(f"  Found coordinates for {host}")
        else:
            print(f"  No WARC records found for {host}")

    return crawl_coordinates


def main(host_index, columnar_index):
    # df = pd.read_parquet("EOT-2020-with-ranks-v5.parquet")
    #
    # print(df["is_us_federal"] == True).sum())
    # df[df["is_us_federal"] == True].head(1)
    #
    # rows_is_us_federal = duckdb.sql(sq2)
    # query_is_us_federal = '''
    #      SELECT DISTINCT url_host_name, url_host_tld, url_host_registered_domain
    #      FROM ccindex
    #      WHERE is_us_federal = True;
    #      '''

    rows_urls = get_urls(columnar_index, query_is_us_federal)

    crawl_coordinates = get_crawls_coordinates(host_index, rows_urls)

    # cdx = cdx_toolkit.CDXFetcher(source='ia')
    #
    # warcinfo = {
    #     'software': 'pypi_cdx_toolkit iter-and-warc example',
    #     'isPartOf': 'WARC2ZIP-COMMONCRAWL',
    #     'description': 'warc extraction',
    #     'format': 'WARC file version 1.1',
    # }
    #
    # writer = cdx_toolkit.warc.get_writer('WARC2ZIP', 'COMMONCRAWL', warcinfo, warc_version='1.0')
    #
    # df = rows_is_us_federal.fetchdf()
    # for _, row in df.iterrows():
    #     url_host_name = row['url_host_name']
    #     url_pattern = f'{url_host_name}/*'
    #     print(f"Pattern {url_pattern}")
    #
    #     for obj in cdx.iter(url_pattern, limit=1, filter=['status:200'], crawl="CC-MAIN-2026-12"):
    #         url = obj['url']
    #         timestamp = obj['timestamp']
    #
    #         print('Extracting url', url, 'timestamp', timestamp)
    #
    #         try:
    #             record = obj.fetch_warc_record()
    #         except RuntimeError:
    #             print(' skipping capture for RuntimeError 404: %s %s', url, timestamp)
    #             continue
    #         writer.write_record(record)
    #
    #         print(' wrote', url)
    #         break  # move to next host after first successful write
    #
    # writer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch WARC records from Common Crawl using host/columnar indexes")
    parser.add_argument(
        "--host-index",
        type=str,
        metavar="DIRECTORY",
        help="Path to directory containing host-level index parquet files",
        required=True,
    )
    parser.add_argument(
        "--columnar-index", type=str, metavar="FILE", help="Path to columnar index parquet file", required=True
    )
    args = parser.parse_args()

    main(args.host_index, args.columnar_index)
