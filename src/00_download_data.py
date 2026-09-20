"""
Step 0 - Acquire the source event log.

The project is documented against the Kaggle publication of this dataset:
  https://www.kaggle.com/datasets/albertopmd/process-mining-event-log-incident-management

That Kaggle entry is a republication of the original UCI ML Repository donation
(dataset 498, "Incident management process enriched event log"). We download from
UCI because it needs no API credentials, which keeps the pipeline reproducible for
anyone who clones this repo. The file contents are identical.

Usage:
    python src/00_download_data.py
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

UCI_URL = (
    "https://archive.ics.uci.edu/static/public/498/"
    "incident+management+process+enriched+event+log.zip"
)
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
ZIP_PATH = RAW_DIR / "incident_event_log.zip"
CSV_PATH = RAW_DIR / "incident_event_log.csv"

EXPECTED_ROWS = 141_712
EXPECTED_COLUMNS = 36


def download(url: str, dest: Path) -> None:
    """Stream the archive to disk, failing loudly rather than leaving a partial file."""
    print(f"Downloading {url}")
    try:
        with urlopen(url, timeout=120) as response:
            if response.status != 200:
                raise RuntimeError(f"Unexpected HTTP status {response.status}")
            payload = response.read()
    except URLError as exc:
        raise RuntimeError(
            "Could not reach the UCI archive. Check your network, or download the "
            "Kaggle copy manually into data/raw/incident_event_log.csv."
        ) from exc

    dest.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    print(f"  saved {dest.name} ({len(payload):,} bytes)")
    print(f"  sha256 {digest}")


def extract(archive: Path, dest_dir: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        members = zf.namelist()
        print(f"  archive contains {members}")
        zf.extractall(dest_dir)


def verify(csv_path: Path) -> None:
    """Cheap structural check so a corrupt download fails here, not three steps later."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Expected {csv_path} after extraction")

    with csv_path.open("r", encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split(",")
        row_count = sum(1 for _ in fh)

    print(f"  columns: {len(header)} (expected {EXPECTED_COLUMNS})")
    print(f"  data rows: {row_count:,} (expected {EXPECTED_ROWS:,})")

    if len(header) != EXPECTED_COLUMNS:
        raise ValueError(f"Column count mismatch: got {len(header)}")
    if row_count != EXPECTED_ROWS:
        raise ValueError(f"Row count mismatch: got {row_count:,}")


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if CSV_PATH.exists():
        print(f"{CSV_PATH} already present - skipping download.")
    else:
        download(UCI_URL, ZIP_PATH)
        extract(ZIP_PATH, RAW_DIR)

    verify(CSV_PATH)
    print("Step 0 complete: raw event log is present and structurally valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
