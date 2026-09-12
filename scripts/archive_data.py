"""Archive the irreplaceable historical co-scientist data and verify it byte-for-byte.

The git snapshot does NOT contain the historical `runs/` trees (they are gitignored
inside each engine root), so the archive this script creates is the only copy until it
is committed. Everything here is deliberately conservative: files are copied as opaque
bytes (`shutil.copy2`, no decode, no mojibake repair) and every copy is re-hashed
against the manifest before the run is declared OK.

Usage (from anywhere):
    python scripts/archive_data.py create        # copy, write manifest, verify
    python scripts/archive_data.py verify        # re-hash the archive against the manifest
    python scripts/archive_data.py add-optional <path-relative-to-archive>

Optional entries (e.g. the gitignored database dump) are recorded in the manifest but
verified only when present, and never counted toward the `ARCHIVE OK <N>` total.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = PROJECT_ROOT / "archive"
MANIFEST_PATH = ARCHIVE_ROOT / "MANIFEST.sha256"

MANIFEST_HEADER = "# path\tsha256\tsize_bytes"
OPTIONAL_MARKER = "# --- optional local artifacts (gitignored; verified only when present) ---"

# Repo plumbing that lives in the archive but is not archived data, so it is neither
# hashed nor counted: the manifest itself, and the ignore/attribute rules that keep the
# archive trackable and byte-exact.
CONTROL_FILES = frozenset({"MANIFEST.sha256", ".gitignore", ".gitattributes"})

# Historical run trees -> archive/imported-runs/<label>/
RUN_SOURCES: tuple[tuple[str, str], ...] = (
    ("v1", "ai-coscientist/runs"),
    ("v2", "ai-coscientist_v2/runs"),
    ("gui", "engines/v1/runs"),
)

# Engine source + prompt assets -> archive/engine-source/<label>/
# Paths are relative to the engine root; entries that do not exist in a given root are
# skipped (e.g. _ingest.py only exists in v2). Secrets (.mcp.json, settings.local.json)
# are deliberately excluded.
SOURCE_ROOTS: tuple[tuple[str, str], ...] = (
    ("v1", "ai-coscientist"),
    ("v2", "ai-coscientist_v2"),
)
SOURCE_ASSETS: tuple[str, ...] = (
    ".claude/agents",
    ".claude/skills/coscientist/SKILL.md",
    "co-scientist_principles.md",
    "cartographer_graft_spec.md",
    "CLAUDE.md",
    "README.md",
    "Data/consumer_software_category_map.json",
    "glow_guardian_brief.md",
    "_ingest.py",
    "make_ideas_csv.py",
    "coscientist.py",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def copy_tree(src: Path, dest: Path) -> int:
    """Byte-exact copy of every file under src. Returns the file count."""
    if not src.exists():
        raise SystemExit(f"FATAL: source tree missing: {src}")
    copied = 0
    for path in iter_files(src):
        if path.is_symlink():
            raise SystemExit(f"FATAL: refusing to copy symlink: {path}")
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def copy_asset(src: Path, dest: Path) -> int:
    if src.is_dir():
        return copy_tree(src, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return 1


def read_manifest() -> tuple[list[tuple[str, str, int]], list[tuple[str, str, int]]]:
    """Returns (required, optional) entries of (posix_path, sha256, size)."""
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"FATAL: manifest missing: {MANIFEST_PATH}")
    required: list[tuple[str, str, int]] = []
    optional: list[tuple[str, str, int]] = []
    bucket = required
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip() == OPTIONAL_MARKER:
            bucket = optional
            continue
        if not line.strip() or line.startswith("#"):
            continue
        path, digest, size = line.split("\t")
        bucket.append((path, digest, int(size)))
    return required, optional


def write_manifest(entries: list[tuple[str, str, int]], optional: list[tuple[str, str, int]]) -> None:
    lines = [MANIFEST_HEADER]
    lines += [f"{path}\t{digest}\t{size}" for path, digest, size in entries]
    if optional:
        lines.append(OPTIONAL_MARKER)
        lines += [f"{path}\t{digest}\t{size}" for path, digest, size in optional]
    MANIFEST_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def archive_data_files(optional_paths: set[str]) -> list[Path]:
    return [
        path
        for path in iter_files(ARCHIVE_ROOT)
        if path.relative_to(ARCHIVE_ROOT).as_posix() not in CONTROL_FILES | optional_paths
    ]


def scan_archive(optional_paths: set[str]) -> list[tuple[str, str, int]]:
    return sorted(
        (path.relative_to(ARCHIVE_ROOT).as_posix(), sha256_of(path), path.stat().st_size)
        for path in archive_data_files(optional_paths)
    )


def cmd_create() -> int:
    if ARCHIVE_ROOT.exists():
        raise SystemExit(
            f"FATAL: {ARCHIVE_ROOT} already exists. Refusing to overwrite an existing "
            "archive; inspect it and use 'verify' instead."
        )

    total_run_files = 0
    for label, rel in RUN_SOURCES:
        src = PROJECT_ROOT / rel
        dest = ARCHIVE_ROOT / "imported-runs" / label
        count = copy_tree(src, dest)
        total_run_files += count
        print(f"  runs   {label:<4} {count:>4} files  <- {rel}")

    total_source_files = 0
    for label, rel in SOURCE_ROOTS:
        root = PROJECT_ROOT / rel
        dest_root = ARCHIVE_ROOT / "engine-source" / label
        for asset in SOURCE_ASSETS:
            src = root / asset
            if not src.exists():
                continue
            total_source_files += copy_asset(src, dest_root / asset)
        print(f"  source {label:<4} {total_source_files:>4} files cumulative  <- {rel}")

    entries = scan_archive(optional_paths=set())
    write_manifest(entries, optional=[])
    print(f"  manifest: {len(entries)} entries "
          f"({total_run_files} run files, {total_source_files} source files)")
    return cmd_verify()


def cmd_verify() -> int:
    required, optional = read_manifest()
    optional_paths = {path for path, _, _ in optional}

    problems: list[str] = []
    for rel, digest, size in required:
        path = ARCHIVE_ROOT / rel
        if not path.is_file():
            problems.append(f"MISSING   {rel}")
            continue
        actual_size = path.stat().st_size
        if actual_size != size:
            problems.append(f"SIZE      {rel}: manifest {size}, actual {actual_size}")
            continue
        actual = sha256_of(path)
        if actual != digest:
            problems.append(f"HASH      {rel}: manifest {digest}, actual {actual}")

    manifest_paths = {rel for rel, _, _ in required}
    on_disk = {
        p.relative_to(ARCHIVE_ROOT).as_posix()
        for p in archive_data_files(optional_paths)
    }
    for extra in sorted(on_disk - manifest_paths):
        problems.append(f"UNTRACKED {extra} (present in archive, absent from manifest)")

    skipped = 0
    for rel, digest, size in optional:
        path = ARCHIVE_ROOT / rel
        if not path.is_file():
            print(f"  optional {rel}: not present locally (skipped)")
            skipped += 1
            continue
        actual_size = path.stat().st_size
        actual = sha256_of(path)
        if actual_size != size or actual != digest:
            problems.append(f"OPTIONAL  {rel}: hash/size mismatch")
        else:
            print(f"  optional {rel}: OK ({size} bytes)")

    if problems:
        print(f"ARCHIVE FAILED — {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    total_bytes = sum(size for _, _, size in required)
    print(f"  verified {len(required)} files, {total_bytes} bytes "
          f"({total_bytes / 1048576:.2f} MiB), {skipped} optional entr(y/ies) skipped")
    print(f"ARCHIVE OK {len(required)} files")
    return 0


def cmd_add_optional(rel: str) -> int:
    path = ARCHIVE_ROOT / rel
    if not path.is_file():
        raise SystemExit(f"FATAL: not a file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise SystemExit(f"FATAL: refusing to record an empty file: {path}")
    required, optional = read_manifest()
    optional = [entry for entry in optional if entry[0] != rel]
    optional.append((rel, sha256_of(path), size))
    write_manifest(required, sorted(optional))
    print(f"  recorded optional artifact {rel} ({size} bytes)")
    return cmd_verify()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="copy the source trees, write the manifest, verify")
    sub.add_parser("verify", help="re-hash the archive against the manifest")
    add_optional = sub.add_parser("add-optional", help="record a gitignored local artifact")
    add_optional.add_argument("path", help="path relative to the archive root")
    args = parser.parse_args()

    if args.command == "create":
        return cmd_create()
    if args.command == "verify":
        return cmd_verify()
    return cmd_add_optional(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
