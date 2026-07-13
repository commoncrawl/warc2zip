import argparse
import logging
import os
import sys
import threading
import time

import duckdb

log = logging.getLogger("host_index_to_coordinates")


def tail_progress(path, stop_event, interval):
    """Log a running coordinate count by watching the output CSV grow.

    DuckDB's CSV COPY flushes incrementally, so we can count newlines in the bytes appended
    since the last tick (cheap: only the new tail is read, never the whole file). The first
    line is the CSV header, so it is subtracted from the reported coordinate count.
    """
    pos = 0
    lines = 0
    while not stop_event.wait(interval):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue  # file not created yet
        if size <= pos:
            continue
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        pos += len(chunk)
        lines += chunk.count(b"\n")
        log.info("  ... %d coordinates so far (%.1f MB written)", max(lines - 1, 0), size / 1e6)


def confirm(prompt):
    """Ask a yes/no question on stderr (keeps stdout clean for CSV). Returns True on yes."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        return input().strip().lower() in ("y", "yes")
    except EOFError:
        return False


def build_sql(host_index, columnar_index, limit=None, homepage=False):
    """Build the single-pass JOIN that maps US-federal hosts to their WARC coordinates.

    The EOT host index only tells us *which* hosts are US-federal; the exact
    warc_filename/offset/length live in the Common Crawl columnar index. We semi-join
    the ~29k filtered hosts against the columnar index in one streaming pass (rather
    than one query per host) and emit the three coordinate columns.
    """
    limit_clause = f"LIMIT {limit}" if limit else ""
    # DNS/crawldiagnostics pseudo-records are excluded by `subset = 'warc'`; --homepage
    # further restricts to each host's root path.
    homepage_clause = "AND ci.url_path = '/'" if homepage else ""

    return f"""
        WITH hosts AS (
            SELECT DISTINCT url_host_name, url_host_tld, url_host_registered_domain
            FROM read_parquet('{host_index}')
            WHERE is_us_federal = True
            {limit_clause}
        )
        SELECT DISTINCT ci.warc_filename, ci.warc_record_offset, ci.warc_record_length
        FROM read_parquet('{columnar_index}', hive_partitioning = true) ci
        JOIN hosts h
          USING (url_host_name, url_host_tld, url_host_registered_domain)
        WHERE ci.subset = 'warc'
        {homepage_clause}
        ORDER BY ci.warc_filename, ci.warc_record_offset
    """


def execute_with_retries(sql, retries=100):
    """Execute `sql`, retrying transient S3 read failures (mirrors build_custom_warc.get_urls).

    Uses `duckdb.execute` (not `duckdb.sql`) so a COPY statement runs eagerly and returns a
    cursor whose single row is the number of rows written.
    """
    retries_left = retries
    while True:
        try:
            return duckdb.execute(sql)
        except (duckdb.HTTPException, duckdb.InvalidInputException) as e:
            # e.g. HTTP 403 on the columnar index, or "No magic bytes found at end of file".
            log.warning("read_parquet exception (%d retries left): %r", retries_left, e)
            if retries_left:
                log.warning("sleeping for 60s before retry")
                time.sleep(60)
                retries_left -= 1
            else:
                raise


def configure_aws_profile(profile):
    """Register an S3 secret that resolves credentials from a named AWS profile.

    Uses DuckDB's credential_chain provider (reads ~/.aws/config & credentials), matching
    the AWS SDK's resolution. Needed when --columnar-index points at s3://commoncrawl/...
    """
    log.info("Loading httpfs + aws extensions")
    duckdb.sql("INSTALL httpfs; LOAD httpfs;")
    duckdb.sql("INSTALL aws; LOAD aws;")
    log.info("Registering S3 secret from AWS profile %r", profile)
    duckdb.sql(
        f"""
        CREATE OR REPLACE SECRET cc_profile (
            TYPE s3,
            PROVIDER credential_chain,
            CHAIN config,
            PROFILE '{profile}'
        );
        """
    )


def count_hosts(host_index, limit=None):
    """Count the distinct US-federal hosts to look up (cheap query on the small EOT file)."""
    limit_clause = f"LIMIT {limit}" if limit else ""
    sql = f"""
        SELECT count(*) FROM (
            SELECT DISTINCT url_host_name, url_host_tld, url_host_registered_domain
            FROM read_parquet('{host_index}')
            WHERE is_us_federal = True
            {limit_clause}
        )
    """
    return execute_with_retries(sql).fetchone()[0]


def main(host_index, columnar_index, output, limit=None, homepage=False, profile=None,
         progress_interval=15, assume_yes=False):
    duckdb.sql("SET enable_progress_bar = true;")
    duckdb.sql("SET http_retries = 100;")

    if profile:
        configure_aws_profile(profile)

    log.info("EOT host index:  %s", host_index)
    log.info("Columnar index:  %s", columnar_index)

    n_hosts = count_hosts(host_index, limit=limit)
    log.info("US-federal hosts to look up: %d%s", n_hosts, " (homepage only)" if homepage else "")

    # Gate the expensive columnar-index scan behind a confirmation when run interactively.
    if not assume_yes and sys.stdin.isatty():
        if not confirm(f"Scan the columnar index for {n_hosts} hosts? This can be slow/costly. [y/N] "):
            log.info("Aborted by user.")
            return

    inner = build_sql(host_index, columnar_index, limit=limit, homepage=homepage)

    # `/dev/stdout` keeps everything inside DuckDB (no pandas/numpy) for both sinks.
    destination = "/dev/stdout" if output in (None, "-") else output
    copy_sql = f"COPY ({inner}) TO '{destination}' (FORMAT CSV, HEADER);"

    # Live coordinate count by tailing the growing CSV — only meaningful for a real file.
    monitor, stop_event = None, threading.Event()
    if progress_interval > 0 and destination != "/dev/stdout":
        monitor = threading.Thread(
            target=tail_progress, args=(destination, stop_event, progress_interval), daemon=True
        )
        monitor.start()

    log.info("Scanning columnar index and writing coordinates to %s ...", destination)
    start = time.perf_counter()
    try:
        cursor = execute_with_retries(copy_sql)
    finally:
        stop_event.set()
        if monitor is not None:
            monitor.join(timeout=2)
    elapsed = time.perf_counter() - start

    # COPY reports the number of rows written; log it (stdout stays clean CSV).
    row = cursor.fetchone()
    nb_rows = row[0] if row is not None else "?"
    log.info("Done: wrote %s coordinate rows to %s in %.1fs", nb_rows, destination, elapsed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Map US-federal hosts from an EOT host index to Common Crawl WARC coordinates (CSV)."
    )
    parser.add_argument(
        "input_file",
        type=str,
        metavar="EOT_HOST_INDEX",
        help="Path/URI to the EOT host-index parquet (e.g. EOT-2020-with-ranks-v5.parquet)",
    )
    parser.add_argument(
        "--columnar-index",
        type=str,
        required=True,
        metavar="GLOB_OR_DIR",
        help=(
            "Common Crawl columnar-index parquet glob or directory. Local "
            "(e.g. '~/cc-index/**/*.parquet') or S3 "
            "(e.g. 's3://commoncrawl/cc-index/table/cc-main/warc/*.parquet')."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default="-",
        help="Output CSV path, or '-' for stdout (default: stdout)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum number of hosts to process"
    )
    parser.add_argument(
        "--homepage",
        action="store_true",
        help="Restrict to each host's homepage (url_path = '/')",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        metavar="AWS_PROFILE",
        help="AWS profile name for S3 access to the columnar index (default: default credential chain)",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=15,
        metavar="SECONDS",
        help="Log a running coordinate count every N seconds when writing to a file; 0 disables (default: 15)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before scanning the columnar index",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress logging on stderr (only warnings/errors)",
    )
    args = parser.parse_args()

    # Logs go to stderr with timestamps so stdout stays a clean, pipeable CSV.
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    main(
        args.input_file,
        args.columnar_index,
        args.output,
        limit=args.limit,
        homepage=args.homepage,
        profile=args.profile,
        progress_interval=args.progress_interval,
        assume_yes=args.yes,
    )
