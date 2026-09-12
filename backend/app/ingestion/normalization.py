"""Text normalization: cleans extraction artifacts without touching meaning.

Deliberately conservative - this stage exists to fix things parsers
introduce or that source files carry incidentally, not to rewrite content:

- Null bytes / other C0 control chars (except \\t\\n\\r): never legitimate in
  extracted text; Postgres text columns reject \\x00 outright, and no font
  ever encodes meaning in a control byte pypdf/python-docx would surface.
- NFKC normalization: folds visually-identical/compatibility characters
  (including turning a non-breaking space into a regular space) so text
  compares and embeds consistently regardless of source encoding quirks.
- Windows/Mac line endings: normalized to \\n.
- 3+ blank lines collapsed to 2 (one visible paragraph break) - PDF text
  extraction routinely emits runs of blank lines from empty layout regions;
  collapsing them isn't a content change, it's removing an extraction
  artifact.
- Runs of 2+ spaces/tabs collapsed to one, and trailing whitespace per line.

Never lowercases, never strips punctuation, never removes stopwords - that's
retrieval-time normalization, if any, and belongs in Phase 5's query/index
pipeline, not here where it would permanently discard information from the
stored chunk.
"""

import re
import unicodedata

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRAILING_WHITESPACE_RE = re.compile(r"[ \t]+\n")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_EXCESS_SPACES_RE = re.compile(r"[ \t]{2,}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _TRAILING_WHITESPACE_RE.sub("\n", text)
    text = _EXCESS_SPACES_RE.sub(" ", text)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
