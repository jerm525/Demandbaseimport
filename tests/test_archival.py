import os
import time

from demandbase_sync.archival import archive_failure, archive_success, purge_old_files


def test_archive_success_moves_file(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    archive = tmp_path / "archive"
    src = staging / "file.csv"
    src.write_text("data")

    dest = archive_success(src, archive)
    assert not src.exists()
    assert dest.exists()
    assert dest.parent == archive


def test_archive_failure_moves_file(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    error = tmp_path / "error"
    src = staging / "file.csv"
    src.write_text("data")

    dest = archive_failure(src, error)
    assert not src.exists()
    assert dest.exists()


def test_purge_old_files_removes_only_old_ones(tmp_path):
    # TC5: files older than retention removed; newer files untouched.
    archive = tmp_path / "archive"
    archive.mkdir()
    old_file = archive / "old.csv"
    new_file = archive / "new.csv"
    old_file.write_text("old")
    new_file.write_text("new")

    ninety_one_days_ago = time.time() - (91 * 86400)
    os.utime(old_file, (ninety_one_days_ago, ninety_one_days_ago))

    deleted = purge_old_files([archive], retention_days=90)

    assert old_file not in deleted or not old_file.exists()
    assert not old_file.exists()
    assert new_file.exists()
