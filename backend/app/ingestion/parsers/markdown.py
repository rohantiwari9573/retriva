"""Markdown parsing.

Markdown headings are a well-defined, regex-stable ATX syntax (`#`..`######`
at line start) - pulling in a full Markdown-to-HTML/AST library (mistune,
markdown-it-py) would mean rendering the document just to throw the HTML
away and re-extract plain text, which is more moving parts for the same
result. Non-heading lines are grouped into elements, each tagged with the
heading path ("H1 > H2 > H3", "where practical" per the section-metadata
spec) of the nearest preceding headings at each level.

Memory: the file is decoded in blocks against the document's text budget and
consumed line by line (never one list of every line: 25 MB of one-character
lines made millions of string objects, ~420 MiB, before the length check). An
element's lines are joined into one StringIO as they arrive, so its memory is
its text (bounded by the text budget), not one object per line. Elements are
exactly what the original whole-text implementation produced.
"""

import io
import re
from collections.abc import Iterator
from typing import BinaryIO

from app.ingestion.parsers.base import ParsedDocument, ParsedElement, TextBudget, iter_utf8_text

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _lines(blocks: Iterator[str]) -> Iterator[str]:
    """The same lines str.splitlines() gives for the whole text, produced block by
    block. The last piece of a block is held back unless it ends in a line break,
    and a trailing "\\r" is held back too, since its "\\n" may start the next block."""
    pending = ""
    for block in blocks:
        pending += block
        lines = pending.splitlines(keepends=True)
        if lines:
            last = lines[-1]
            complete = last.splitlines()[0] != last and not last.endswith("\r")
            pending = "" if complete else lines.pop()
        for line in lines:
            yield line.splitlines()[0]
    if pending:
        yield from pending.splitlines()


class MarkdownParser:
    def parse(self, data: bytes) -> ParsedDocument:
        return self.parse_stream(io.BytesIO(data))

    def parse_file(self, path: str) -> ParsedDocument:
        with open(path, "rb") as file:
            return self.parse_stream(file)

    def parse_stream(self, file: BinaryIO) -> ParsedDocument:
        elements: list[ParsedElement] = []
        heading_stack: list[str] = []  # index i = heading text at level i+1
        # The current element's "\n".join(lines), built incrementally.
        buffer = io.StringIO()
        has_lines = False

        def flush() -> None:
            nonlocal buffer, has_lines
            content = buffer.getvalue().strip()
            buffer = io.StringIO()
            has_lines = False
            if content:
                titled = [h for h in heading_stack if h]
                section = " > ".join(titled) if titled else None
                elements.append(ParsedElement(text=content, section=section))

        for line in _lines(iter_utf8_text(file, TextBudget())):
            match = _HEADING_RE.match(line)
            if match:
                flush()
                level = len(match.group(1))
                title = match.group(2).strip()
                del heading_stack[level - 1 :]
                while len(heading_stack) < level - 1:
                    heading_stack.append("")
                heading_stack.append(title)
            else:
                if has_lines:
                    buffer.write("\n")
                buffer.write(line)
                has_lines = True
        flush()

        return ParsedDocument(elements=elements)
