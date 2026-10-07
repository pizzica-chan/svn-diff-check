"""Validate SVN file content changes against expected +/- lines (Python 3.10+)."""
from __future__ import annotations

import argparse
import csv
import difflib
import io
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class CheckError(Exception):
    """Invalid configuration, unsupported diff, or SVN execution failure."""


@dataclass(frozen=True)
class Check:
    line: int
    staging: str
    production: str
    expected: Path


def load_checks(path: Path) -> list[Check]:
    checks = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t", strict=True)
        columns = ["staging_path", "production_path", "expected_path"]
        if reader.fieldnames != columns:
            raise CheckError("TSV header must be: " + "\t".join(columns))
        for row in reader:
            if None in row or any(value is None or not value.strip() for value in row.values()):
                raise CheckError(f"TSV line {reader.line_num}: requires exactly three non-empty fields")
            staging, production, expected = (row[key].strip() for key in columns)
            for url in (staging, production):
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https", "svn", "svn+ssh", "file"} or (parsed.scheme != "file" and not parsed.netloc) or not parsed.path:
                    raise CheckError(f"TSV line {reader.line_num}: requires a full SVN file URL: {url}")
            checks.append(Check(reader.line_num, staging, production, path.parent / expected))
    if not checks:
        raise CheckError("TSV contains no checks")
    return checks


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def text_lines(text: str) -> list[str]:
    """Split physical lines without treating Unicode content as line breaks."""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def extract_changes(diff: str) -> list[str]:
    """Parse text hunks; preserve signs, whitespace, order, and duplicate lines."""
    changes = []
    old_left = new_left = 0
    for line in text_lines(diff):
        if line.startswith("Cannot display:") or line.startswith("Binary files "):
            raise CheckError("Binary diff is unsupported; use text configuration files")
        match = HUNK.match(line)
        if match:
            if old_left or new_left:
                raise CheckError("Incomplete SVN diff hunk")
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
        elif old_left or new_left:
            if line.startswith("\\ No newline at end of file"):
                continue
            if line.startswith("-"):
                changes.append(line)
                old_left -= 1
            elif line.startswith("+"):
                changes.append(line)
                new_left -= 1
            elif line.startswith(" "):
                old_left -= 1
                new_left -= 1
            else:
                raise CheckError("Malformed SVN diff hunk")
            if old_left < 0 or new_left < 0:
                raise CheckError("Invalid SVN diff hunk counts")
        elif line and not (
            line.startswith(("Index: ", "--- ", "+++ "))
            or set(line) == {"="}
            or line == "\\ No newline at end of file"
        ):
            raise CheckError(f"Unrecognized SVN diff output: {line}")
    if old_left or new_left:
        raise CheckError("Incomplete SVN diff hunk")
    return changes


def load_expected(path: Path) -> list[str]:
    lines = text_lines(path.read_text(encoding="utf-8-sig"))
    if any(not line.startswith(("+", "-")) for line in lines):
        raise CheckError(f"Expected file must contain only +/- lines: {path}")
    return lines


def get_svn_diff(check: Check, executable: str, revision: str | None,
                 encoding: str, timeout: float) -> list[str]:
    command = [executable, "diff", "--non-interactive", "--internal-diff", "--ignore-properties",
               "--extensions", ""]  # Override configured whitespace-ignore options.
    if revision:
        if any("@" in url and url.rpartition("@")[2] for url in (check.staging, check.production)):
            raise CheckError("Do not combine --revision with URL @revision; choose one revision method")
        if ":" not in revision:
            revision = f"{revision}:{revision}"
        command += ["--revision", revision]
    command += ["--old=" + check.staging, "--new=" + check.production]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding=encoding, errors="strict", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(encoding, errors="replace")
        raise CheckError(f"SVN timed out after {timeout:g}s; stderr: {stderr.strip()}") from exc
    except (OSError, UnicodeError) as exc:
        raise CheckError(f"Cannot execute/decode SVN: {exc}") from exc
    if result.returncode:
        raise CheckError(f"SVN exit {result.returncode}; stderr: {result.stderr.strip() or '(empty)'}")
    if result.stderr.strip():
        print("  SVN stderr: " + result.stderr.strip(), file=sys.stderr)
    return extract_changes(result.stdout)


def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tsv", type=Path, help="TSV comparison definitions")
    parser.add_argument("--svn", default="svn", help="SVN executable (default: svn)")
    parser.add_argument("--revision", help="Common operative revision N or N:M; numeric revisions recommended for CI")
    parser.add_argument("--encoding", default="utf-8", help="SVN output encoding (default: utf-8)")
    parser.add_argument("--timeout", type=positive_timeout, default=60.0, help="Seconds per SVN call (default: 60)")
    args = parser.parse_args(argv)
    try:
        import codecs
        codecs.lookup(args.encoding)
        with io.TextIOWrapper(io.BytesIO(), encoding=args.encoding):
            pass  # Validate the same text codec requirement used by subprocess.
        if args.revision and not re.fullmatch(r"(?:\d+|HEAD)(?::(?:\d+|HEAD))?", args.revision):
            raise CheckError("--revision must be N, HEAD, or N:M")
        checks = load_checks(args.tsv.resolve())
    except (CheckError, OSError, UnicodeError, csv.Error, ValueError, LookupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    passed = mismatched = errors = 0
    for check in checks:
        print(f"CHECK line {check.line}: {check.staging} -> {check.production}")
        print(f"  Expected: {check.expected}")
        try:
            expected = load_expected(check.expected)
            actual = get_svn_diff(check, args.svn, args.revision, args.encoding, args.timeout)
        except (CheckError, OSError, UnicodeError, ValueError) as exc:
            print(f"  ERROR line {check.line}: {exc}", file=sys.stderr)
            errors += 1
            continue
        if expected == actual:
            passed += 1
            print(f"  OK ({len(actual)} change lines)")
        else:
            mismatched += 1
            print("  NG: unexpected changes (comparison diff below)")
            print("\n".join(difflib.unified_diff(expected, actual, fromfile="expected", tofile="actual", lineterm="")))
    print(f"SUMMARY: total={len(checks)} OK={passed} NG={mismatched} ERROR={errors}")
    return 2 if errors else 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
