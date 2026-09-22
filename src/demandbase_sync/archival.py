"""File archival (staging -> archive/error) and 90-day retention cleanup."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def archive_success(csv_path: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / csv_path.name
    shutil.move(str(csv_path), str(destination))
    return destination


def archive_failure(csv_path: Path, error_dir: Path) -> Path:
    error_dir.mkdir(parents=True, exist_ok=True)
    destination = error_dir / csv_path.name
    if csv_path.exists():
        shutil.move(str(csv_path), str(destination))
    return destination


def purge_old_files(directories: List[Path], retention_days: int) -> List[Path]:
    """Delete files older than `retention_days` from each directory in
    `directories` (normally archive/ and error/). Returns the list of
    deleted paths, for the caller to log."""
    cutoff = time.time() - (retention_days * 86400)
    deleted: List[Path] = []
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
    if deleted:
        logger.info("Retention cleanup deleted %d file(s): %s", len(deleted), [str(p) for p in deleted])
    return deleted
