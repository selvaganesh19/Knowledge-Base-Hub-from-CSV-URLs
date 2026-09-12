"""Tests for the text chunker."""

from __future__ import annotations

from app.services.chunking import chunk_text, split_blocks, split_long_block


def test_empty_input_produces_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk():
    chunks = chunk_text("Tim Cook is the Chief Executive Officer of Apple.")

    assert len(chunks) == 1
    assert chunks[0]["ordinal"] == 0
    assert chunks[0]["char_start"] == 0


def test_chunks_respect_the_size_limit():
    text = "\n\n".join(f"Paragraph {index} " + "word " * 60 for index in range(12))
    chunks = chunk_text(text, max_chars=600, overlap=80)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk["text"]) <= 600 + 80


def test_oversized_paragraph_is_split():
    paragraph = " ".join(f"Sentence number {index} about leadership." for index in range(60))
    chunks = chunk_text(paragraph, max_chars=400, overlap=50)

    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 450 for chunk in chunks)


def test_long_paragraph_splits_on_sentence_boundaries():
    paragraph = ". ".join(f"Point {index} about the board" for index in range(40)) + "."
    pieces = split_long_block(paragraph, 200)

    # Every piece other than the last should end where a sentence ended, rather
    # than mid-sentence at the character limit.
    assert all(piece.endswith(".") for piece in pieces)
    assert pieces[-1].endswith(".")


def test_consecutive_chunks_overlap():
    text = "\n\n".join("word " * 60 for _ in range(10))
    chunks = chunk_text(text, max_chars=500, overlap=100)

    assert len(chunks) >= 2
    tail = chunks[0]["text"][-60:]
    assert tail.strip()[:20] in chunks[1]["text"]


def test_short_sections_are_packed_together():
    """A page with one heading per person must not become one chunk per person.

    This is the regression that produced 25 fragments from a 2,760-character page
    before the chunker stopped flushing on every heading.
    """
    text = "\n\n".join(
        f"## Person {index}\n\nPerson {index} is a Senior Vice President at the company."
        for index in range(20)
    )
    chunks = chunk_text(text, max_chars=1000, overlap=150)

    assert len(chunks) < 8
    assert all(len(chunk["text"]) > 200 for chunk in chunks)


def test_heading_is_recorded_on_the_chunk():
    text = "# Executive Team\n\nTim Cook leads the company.\n\n## Board\n\nArthur Levinson chairs the board."
    chunks = chunk_text(text, max_chars=1000)

    assert chunks[0]["heading"] == "Executive Team"


def test_ordinals_are_sequential():
    text = "\n\n".join("word " * 80 for _ in range(8))
    chunks = chunk_text(text, max_chars=400, overlap=50)

    assert [chunk["ordinal"] for chunk in chunks] == list(range(len(chunks)))


def test_offsets_point_into_the_source_text():
    text = "# Team\n\nAlice runs engineering.\n\nBob runs finance."
    chunks = chunk_text(text, max_chars=1000)

    for chunk in chunks:
        assert 0 <= chunk["char_start"] <= chunk["char_end"]
        assert chunk["char_end"] <= len(text) + 50


class TestBlockSplitting:
    def test_headings_are_separated_from_paragraphs(self):
        blocks = split_blocks("# Title\n\nSome body text.")

        assert ("heading", "Title") in blocks
        assert ("para", "Some body text.") in blocks

    def test_consecutive_lines_join_into_one_paragraph(self):
        blocks = split_blocks("line one\nline two\n\nsecond paragraph")

        assert ("para", "line one line two") in blocks
        assert ("para", "second paragraph") in blocks
