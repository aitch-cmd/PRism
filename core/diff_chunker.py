"""
Split a unified diff into per-file chunks, then pack the per-file chunks
into review-sized groups so `review_pr` can map-reduce over a huge PR
without blowing past the model's context window.

The chunker is intentionally dumb: it trusts the `diff --git a/... b/...`
boundaries that GitHub emits and does not try to parse hunks. That's what
keeps it reliable on pathological diffs (binary files, renames, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileDiff:
    path: str
    body: str  # raw diff text including the `diff --git` header
    lines: int


@dataclass
class DiffChunk:
    files: list[FileDiff] = field(default_factory=list)
    lines: int = 0

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def text(self) -> str:
        return "\n".join(f.body for f in self.files)


def _parse_path(header_line: str) -> str:
    # `diff --git a/foo/bar.py b/foo/bar.py` → `foo/bar.py`
    parts = header_line.strip().split(" b/")
    if len(parts) == 2:
        return parts[1].strip()
    return header_line.strip()


def split_diff_by_file(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current_header: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_header is None:
            return
        body = "\n".join(current_lines)
        files.append(
            FileDiff(
                path=_parse_path(current_header),
                body=body,
                lines=len(current_lines),
            )
        )

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_header = line
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()
    return files


def chunk_diff(
    diff_text: str,
    max_lines_per_chunk: int = 800,
) -> list[DiffChunk]:
    """
    Group per-file diffs into chunks of up to `max_lines_per_chunk` lines.

    A single file larger than the cap becomes its own chunk (we don't split
    inside a file — that would hand the model half of a function's context).
    """
    files = split_diff_by_file(diff_text)
    chunks: list[DiffChunk] = []
    current = DiffChunk()
    for f in files:
        if current.files and current.lines + f.lines > max_lines_per_chunk:
            chunks.append(current)
            current = DiffChunk()
        current.files.append(f)
        current.lines += f.lines
    if current.files:
        chunks.append(current)
    return chunks
