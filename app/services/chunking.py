"""Splitting cleaned page text into retrievable chunks.

The chunk size is chosen against the embedding model's window rather than picked
as a round number: all-MiniLM-L6-v2 truncates at 256 tokens, and ~1000 characters
of English sits comfortably inside that, so a chunk is never silently cut off
before it reaches the model.

Chunks break on headings and paragraph boundaries. A trailing slice of each chunk
is carried into the next one so a fact that straddles a boundary - a name on one
line and its title on the next - is still retrievable from a single chunk.
"""

from __future__ import annotations

import re

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 150) -> list[dict]:
    """Return chunk dicts with ordinal, text, char offsets, and nearest heading."""
    normalized = normalize(text)
    if not normalized:
        return []

    blocks = split_blocks(normalized)
    chunks: list[dict] = []

    parts: list[str] = []
    length = 0
    heading = ""
    cursor = 0
    start_offset = 0

    def flush() -> None:
        nonlocal parts, length, cursor, start_offset, heading
        if not parts:
            return
        body = "\n\n".join(parts).strip()
        if not body:
            parts, length = [], 0
            return
        chunks.append(
            {
                "ordinal": len(chunks),
                "text": body,
                "char_start": start_offset,
                "char_end": start_offset + len(body),
                "heading": heading,
            }
        )
        parts, length = [], 0

    for kind, block in blocks:
        is_heading = kind == "heading"

        # A heading starts a new chunk only once the current one is reasonably
        # full. Flushing on every heading looks tidy but shreds pages whose
        # sections are short - a leadership page with one heading per executive
        # produced 25 tiny chunks instead of 3 usable ones, which is exactly the
        # fragmentation chunk-based retrieval is supposed to avoid.
        if is_heading and length >= max_chars // 2:
            flush()

        if is_heading and not parts:
            heading = block

        if not is_heading and len(block) > max_chars:
            # A single oversized paragraph (dense bio lists) is split on sentences.
            flush()
            for piece in split_long_block(block, max_chars):
                position = normalized.find(piece, cursor)
                if position >= 0:
                    start_offset = position
                    cursor = position + len(piece)
                chunks.append(
                    {
                        "ordinal": len(chunks),
                        "text": piece,
                        "char_start": start_offset,
                        "char_end": start_offset + len(piece),
                        "heading": heading,
                    }
                )
            continue

        if parts and length + len(block) + 2 > max_chars:
            flush()
            carry = chunks[-1]["text"][-overlap:] if chunks else ""
            position = normalized.find(block, cursor)
            start_offset = position if position >= 0 else cursor
            if len(carry) > 60:
                parts.append(carry)
                length += len(carry) + 2
        else:
            position = normalized.find(block, cursor)
            if position >= 0:
                if not parts:
                    start_offset = position
                cursor = position + len(block)

        parts.append(block)
        length += len(block) + 2

    flush()
    return chunks


def normalize(text: str) -> str:
    """Collapse redundant whitespace and drop noise lines."""
    if not text:
        return ""

    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if len(line) < 3:
            continue
        lines.append(re.sub(r"[ \t]+", " ", line))

    collapsed = "\n".join(lines)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip()


def split_blocks(text: str) -> list[tuple[str, str]]:
    """Split into ('heading'|'para', content) blocks on blank lines."""
    blocks: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if buffer:
            paragraph = " ".join(buffer).strip()
            if paragraph:
                blocks.append(("para", paragraph))
            buffer.clear()

    for line in text.split("\n"):
        if not line:
            flush_buffer()
            continue

        heading_match = HEADING_RE.match(line)
        if heading_match:
            flush_buffer()
            blocks.append(("heading", heading_match.group(2).strip()))
            continue

        buffer.append(line)

    flush_buffer()
    return blocks


def split_long_block(block: str, max_chars: int) -> list[str]:
    """Break an oversized paragraph on sentence boundaries, then on hard length."""
    pieces: list[str] = []
    current = ""

    for sentence in SENTENCE_RE.split(block):
        if current and len(current) + len(sentence) + 1 > max_chars:
            pieces.append(current.strip())
            current = ""
        if len(sentence) > max_chars:
            for index in range(0, len(sentence), max_chars):
                piece = sentence[index : index + max_chars].strip()
                if piece:
                    pieces.append(piece)
            continue
        current = f"{current} {sentence}".strip()

    if current.strip():
        pieces.append(current.strip())
    return [piece for piece in pieces if piece]
