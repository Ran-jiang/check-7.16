"""Literal, normalization-free comparison of document and statute text."""

from __future__ import annotations

from difflib import SequenceMatcher

from .schema import DiffOperation, ExactTextComparison


def compare_exact_text(document_text: str, statute_text: str) -> ExactTextComparison:
    operations = [
        DiffOperation(
            operation=tag,
            document_text=document_text[i1:i2],
            statute_text=statute_text[j1:j2],
        )
        for tag, i1, i2, j1, j2 in SequenceMatcher(
            None, document_text, statute_text, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    return ExactTextComparison(
        exact_match=document_text == statute_text,
        document_text=document_text,
        statute_text=statute_text,
        operations=operations,
    )
