"""Crash tests over the real archives listed in the README's "WARC examples for testing".

Every archive there was written by a different tool (Nutch, cdx_toolkit repackaging, Heritrix,
Browsertrix, ArchiveTeam's megawarc, 2004/2008-era ARC writers), and each one has broken warc2zip
at some point in a way the synthetic fixtures could not: a producer that writes no metadata
records, one that writes only ``resource`` records, one with four warcinfo records, one with
``dns:`` captures and no HTTP layer. The check is deliberately minimal — exit 0, a valid zip, and
the manifest agreeing with the zip members — because the files are the point, not the assertions.

Two tiers, chosen with pytest markers (registered in ``pyproject.toml``):

- **short** — the default and what CI runs. ``--limit`` keeps every run to a few MB over HTTP
  (~3 s each), so all fourteen archives in both formats finish in about a minute.
- **long** — ``pytest -m long``. Streams each archive end to end: 0.4–1 GB each, and the
  ArchiveTeam megawarc is 10 GB, so this is an hour-plus run for a developer's machine, never CI.
  ``-k flat`` or ``-k sidecar`` halves it. Every zip is deleted once checked, so the disk high-water
  mark is one output at a time rather than ~20 GB under pytest's tmp dir.

Both tiers need the network. ``WARC2ZIP_OFFLINE=1`` skips them.
"""

import csv
import io
import os
import posixpath
import subprocess
import sys
import zipfile
from dataclasses import dataclass

import pytest

# Captures per run in the short tier. Small enough that fsspec's first HTTP block (5 MiB) usually
# covers it, large enough that the drain-until-next-capture logic and the request/metadata joins
# see more than one group.
SHORT_LIMIT = 20

# Wall-clock ceiling for one short run: a hung connection must fail CI, not stall it.
SHORT_TIMEOUT = 300

HF = "https://huggingface.co/buckets/commoncrawl/warc2zip-examples/resolve/"
CC = "https://data.commoncrawl.org/"
EOT = "https://eotarchive.s3.amazonaws.com/crawl-data/"


@dataclass(frozen=True)
class Example:
    name: str  # pytest id
    url: str
    producer: str  # what wrote the archive, for the failure message
    size: str  # from the README, for humans
    # Total captures in the file when it has fewer than SHORT_LIMIT: the Browsertrix pages WARC
    # has 3, and the screenshots/text WARCs have 0 because they carry only `resource` records.
    # None means "at least SHORT_LIMIT", which is every real crawl WARC.
    captures: int | None = None


EXAMPLES = [
    Example(
        "cc-500-records",
        HF + "500_RECORDS-REPACKAGE-CC-MAIN-2026-30.warc.gz?download=true",
        "Common Crawl Nutch slice, response+request+metadata",
        "13 MB",
    ),
    Example(
        "cc-homepages",
        HF + "HOMEPAGES-REPACKAGE-CC-MAIN-2026-21.warc.gz?download=true",
        "cdx_toolkit repackage, response records only",
        "1 GB",
    ),
    Example(
        "cc-us-federal",
        HF + "IS_US_FEDERAL-REPACKAGE-CC-MAIN-2025-13.warc.gz?download=true",
        "cdx_toolkit repackage, response records only",
        "427 MB",
    ),
    Example(
        "cc-main-warc",
        CC + "crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/warc/CC-MAIN-20260807101845-20260807131845-00000.warc.gz",
        "Common Crawl Nutch, main WARC",
        "~1 GB",
    ),
    Example(
        "cc-main-crawldiagnostics",
        CC + "crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/crawldiagnostics/CC-MAIN-20260807101845-20260807131845-00000.warc.gz",
        "Common Crawl Nutch, crawldiagnostics (revisit records)",
        "~1 GB",
    ),
    Example(
        "cc-main-robotstxt",
        CC + "crawl-data/CC-MAIN-2026-34/segments/1786091384908.68/robotstxt/CC-MAIN-20260807101845-20260807131845-00000.warc.gz",
        "Common Crawl Nutch, robotstxt (no metadata records)",
        "~1 GB",
    ),
    Example(
        "eot2024-heritrix",
        EOT + "EOT-2024/segments/IA-000/warc/EOT24PRE-20240926172119-crawl804_EOT24PRE-20240926172119-00000.warc.gz",
        "Heritrix / Internet Archive",
        "1 GB",
    ),
    Example(
        "eot2024-nutch-repackage",
        EOT + "EOT-2024/segments/CC-000/warc/EOT-2024-REPACKAGE-CC-MAIN-2024-42-GOV-000000-001.warc.gz",
        "Common Crawl repackage for EOT",
        "~1 GB",
    ),
    Example(
        "eot2024-browsertrix-pages",
        EOT + "EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-20250114215647018-0.warc.gz",
        "Browsertrix, pages",
        "small",
        captures=3,
    ),
    Example(
        "eot2024-browsertrix-screenshots",
        EOT + "EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-screenshots-20250114215649547.warc.gz",
        "Browsertrix, screenshots (resource records only)",
        "small",
        captures=0,
    ),
    Example(
        "eot2024-browsertrix-text",
        EOT + "EOT-2024/segments/WR-000/warc/EOT24WR-0015_20250114215650265-8c53efcc-e2d-0_eot-http-energy-gov-eere-office-energy-efficiency-renewable-energy-manual-20250114215335-8c53efcc-e2d-text-20250114215649747.warc.gz",
        "Browsertrix, extracted text (resource records only)",
        "small",
        captures=0,
    ),
    Example(
        "eot2024-archiveteam-megawarc",
        EOT + "EOT-2024/segments/AT-000/warc/archiveteam_usgovernment_20250131232111_96ad506d_usgovernment_20250131232111_96ad506d.1738361595.megawarc.warc.gz",
        "ArchiveTeam megawarc (several concatenated WARCs, 4 warcinfo records)",
        "10 GB",
    ),
    Example(
        "eot2004-heritrix-arc",
        EOT + "EOT-2004/segments/NARA-000/warc/NARA-PEOT-2004-20041014205819-00000-crawling009-c_NARA-PEOT-2004-20041014205819-00000-crawling009.archive.org.arc.gz",
        "Heritrix ARC/1.1 with dns: records",
        "~100 MB",
    ),
    Example(
        "ccf2008-arc",
        CC + "crawl-001/2008/06/19/0/1213886083018_0.arc.gz",
        "Common Crawl 2008 ARC",
        "~100 MB",
    ),
]

FORMATS = ["flat", "sidecar"]

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(bool(os.environ.get("WARC2ZIP_OFFLINE")), reason="WARC2ZIP_OFFLINE is set"),
]


def run_warc2zip(example, output, output_format, limit=None, timeout=None):
    """Run the CLI as a subprocess (the exit code is what a crash test is about) and return the
    completed process. Stdout/stderr are captured so a failure shows the tool's own summary."""
    cmd = [sys.executable, "-m", "warc2zip", example.url, "--output", str(output), "--format", output_format]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def check_output(example, proc, output, captures, exact=True):
    """Exit 0, a zip that passes testzip(), one root directory, and a manifest whose row count
    matches `captures` (exactly, or as a lower bound when `exact` is False) and whose `filename`
    column agrees with the members: every response row names a payload file, no revisit row does.
    The manifest is the CSV-only user's index into the zip, so that agreement is the one property
    worth checking on every producer's output."""
    context = f"{example.name} ({example.producer}, {example.size})\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    assert proc.returncode == 0, f"exit {proc.returncode}: {context}"
    assert output.exists(), f"no output zip: {context}"
    assert output.stat().st_size > 0, f"empty zip: {context}"

    with zipfile.ZipFile(output) as zf:
        assert zf.testzip() is None, f"corrupt member in zip: {context}"
        names = zf.namelist()
        roots = {name.split("/", 1)[0] for name in names}
        assert len(roots) == 1, f"expected one root directory, got {sorted(roots)}: {context}"
        (root,) = roots
        manifest = list(csv.DictReader(io.TextIOWrapper(zf.open(f"{root}/manifest.csv"), encoding="utf-8")))

    if exact:
        assert len(manifest) == captures, f"{len(manifest)} manifest rows, expected {captures}: {context}"
    else:
        assert len(manifest) >= captures, f"{len(manifest)} manifest rows, expected at least {captures}: {context}"

    # `filename` is a bare name in both formats; sidecar mode nests it under a domain directory,
    # and a sidecar member's basename ("1000000.html.request.json") never equals a payload's.
    basenames = {posixpath.basename(name) for name in names}
    for row in manifest:
        if row["warc_type"] == "response":
            assert row["filename"] in basenames, f"manifest names {row['filename']}, not in the zip: {context}"
        else:
            assert row["filename"] not in basenames, f"revisit row {row['filename']} has a payload: {context}"


@pytest.mark.parametrize("output_format", FORMATS)
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_short(example, output_format, tmp_path):
    """The CI tier: the first SHORT_LIMIT captures of every README archive, both formats."""
    output = tmp_path / "out.zip"
    proc = run_warc2zip(example, output, output_format, limit=SHORT_LIMIT, timeout=SHORT_TIMEOUT)
    captures = SHORT_LIMIT if example.captures is None else min(example.captures, SHORT_LIMIT)
    check_output(example, proc, output, captures)


@pytest.mark.long
@pytest.mark.parametrize("output_format", FORMATS)
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_long(example, output_format, tmp_path):
    """The full-file tier, ``pytest -m long``. See the module docstring for the cost."""
    output = tmp_path / "out.zip"
    try:
        proc = run_warc2zip(example, output, output_format)
        if example.captures is None:
            # A real crawl WARC holds thousands of captures; pinning the number buys nothing.
            check_output(example, proc, output, SHORT_LIMIT, exact=False)
        else:
            check_output(example, proc, output, example.captures)
    finally:
        # A full-file zip is up to 10 GB; do not leave it under pytest's tmp dir.
        if output.exists():
            output.unlink()
