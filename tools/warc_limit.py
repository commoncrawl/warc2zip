import argparse

from warcio.archiveiterator import ArchiveIterator
from warcio.utils import fsspec_open
from warcio.warcwriter import WARCWriter


def limit_warc(input_file, output_path, limit):
    """Copy the first `limit` captures from a WARC into a new gzipped WARC.

    A "capture" is a `response` record plus its associated `request`/`metadata`
    records (matching warc2zip.py's --limit semantics). The `warcinfo` record is
    preserved but not counted toward the limit.

    Uses the drain-until-next-response pattern (see warc2zip.py:240-257): the Nth
    response sets `limit_reached`, and we keep writing the trailing
    request/metadata records that belong to capture N until the *next* response
    arrives, at which point we stop. A `limit` of 0 (or less) writes only the
    leading `warcinfo` record; a `limit` larger than the file simply copies
    everything.

    Returns the number of response records (captures) written.
    """
    response_count = 0
    limit_reached = False

    with fsspec_open(input_file, "rb") as stream, open(output_path, "wb") as out:
        writer = WARCWriter(out, gzip=True)
        for record in ArchiveIterator(stream):
            if limit_reached and record.rec_type == "response":
                break
            # limit=0: stop before the very first capture, but keep leading warcinfo.
            if limit <= 0 and record.rec_type == "response":
                break
            writer.write_record(record)  # write BEFORE advancing the iterator
            if record.rec_type == "response":
                response_count += 1
                if response_count >= limit:
                    limit_reached = True

    return response_count


def default_output_path(input_file, limit):
    """Derive `foo.warc.gz` -> `foo.limit{N}.warc.gz`."""
    for suffix in (".warc.gz", ".warc", ".gz"):
        if input_file.endswith(suffix):
            return f"{input_file[: -len(suffix)]}.limit{limit}.warc.gz"
    return f"{input_file}.limit{limit}.warc.gz"


def main(input_file, output_path, limit):
    if output_path is None:
        output_path = default_output_path(input_file, limit)

    response_count = limit_warc(input_file, output_path, limit)

    print(f"Wrote {response_count} captures to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a new WARC keeping only the first N captures of an existing WARC"
    )
    parser.add_argument(
        "input_file",
        type=str,
        help="Local path or remote URI (s3://, http://, ...) to a .warc.gz file",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        required=True,
        help="Number of captures (response records) to keep",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output WARC path (default: <input>.limit<N>.warc.gz)",
    )
    args = parser.parse_args()

    main(args.input_file, args.output, args.limit)
