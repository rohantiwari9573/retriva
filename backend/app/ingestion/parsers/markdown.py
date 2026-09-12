"""Markdown parsing.

Markdown headings are a well-defined, regex-stable ATX syntax (`#`..`######`
at line start) - pulling in a full Markdown-to-HTML/AST library (mistune,
markdown-it-py) would mean rendering the document just to throw the HTML
away and re-extract plain text, which is more moving parts for the same
result. Non-heading lines are grouped into elements, each tagged with the
heading path ("H1 > H2 > H3", "where practical" per the section-metadata
spec) of the nearest preceding headings at each level.
"""

import re

from app.ingestion.parsers.base import ParsedDocument, ParsedElement

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class MarkdownParser:
    def parse(self, data: bytes) -> ParsedDocument:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()

        elements: list[ParsedElement] = []
        heading_stack: list[str] = []  # index i = heading text at level i+1
        buffer: list[str] = []

        def flush() -> None:
            content = "\n".join(buffer).strip()
            buffer.clear()
            if content:
                titled = [h for h in heading_stack if h]
                section = " > ".join(titled) if titled else None
                elements.append(ParsedElement(text=content, section=section))

        for line in lines:
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
                buffer.append(line)
        flush()

        return ParsedDocument(elements=elements)
