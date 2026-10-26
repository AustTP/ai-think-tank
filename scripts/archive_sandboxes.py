#!/usr/bin/env python3
"""Archive old sandbox workspaces to tar.gz and delete the originals."""
import argparse
import shutil
import tarfile
import time
from pathlib import Path

DEFAULT_ARCHIVE_DIR = "sandboxes_archive"
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_STAGE_MAX_AGE_HOURS = 2
DEFAULT_ARCHIVE_RETENTION_DAYS = 90
TARGET_PREFIXES = ("story-", "spike-", "review-", "supervisor-")


def archive_old_sandboxes(root, archive_dir, max_age_days, stage_max_age_hours,
                          archive_retention_days, dry_run):
    now = time.time()
    archive_dir.mkdir(exist_ok=True)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        try:
            age_hours = (now - entry.stat().st_mtime) / 3600
        except OSError:
            continue
        if name.startswith("stage-"):
            # A failed stage is a transient retry copy, not a deliverable.
            # Archiving it just moves the same bytes to a tarball; delete it
            # once the resume window has passed.
            if age_hours <= stage_max_age_hours:
                continue
            if dry_run:
                print(f"[DRY RUN] Would delete stage: {entry} ({age_hours:.1f}h old)")
                continue
            print(f"Deleting stage: {entry} ({age_hours:.1f}h old)")
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                print(f"  -> Failed: {exc}")
            continue
        if not name.startswith(TARGET_PREFIXES):
            continue
        if age_hours <= max_age_days * 24:
            continue
        if dry_run:
            print(f"[DRY RUN] Would archive: {entry} ({age_hours:.1f}h old)")
            continue
        tar_path = archive_dir / f"{name}.tar.gz"
        print(f"Archiving: {entry} ({age_hours:.1f}h old)")
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(entry, arcname=name)
            shutil.rmtree(entry)
            print(f"  -> {tar_path}")
        except Exception as exc:
            print(f"  -> Failed: {exc}")
            if tar_path.exists():
                try:
                    tar_path.unlink()
                except OSError:
                    pass

    if archive_retention_days > 0:
        cutoff = now - archive_retention_days * 86400
        for tar_path in archive_dir.glob("*.tar.gz"):
            try:
                if tar_path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if dry_run:
                print(f"[DRY RUN] Would delete old archive: {tar_path}")
                continue
            print(f"Deleting old archive: {tar_path}")
            try:
                tar_path.unlink()
            except OSError as exc:
                print(f"  -> Failed: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Archive old sandboxes to save disk space.")
    parser.add_argument("--root", default="sandboxes", help="Sandbox directory")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE_DIR, help="Archive directory")
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                        help="Age in days to archive story/spike/review/supervisor dirs")
    parser.add_argument("--stage-max-age-hours", type=float, default=DEFAULT_STAGE_MAX_AGE_HOURS,
                        help="Age in hours after which a failed stage is deleted")
    parser.add_argument("--archive-retention-days", type=float, default=DEFAULT_ARCHIVE_RETENTION_DAYS,
                        help="Delete archived tarballs older than this many days")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be archived")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"Root directory not found: {root}")
        return
    archive_old_sandboxes(root, Path(args.archive), args.max_age_days,
                          args.stage_max_age_hours, args.archive_retention_days,
                          args.dry_run)


if __name__ == "__main__":
    main()
