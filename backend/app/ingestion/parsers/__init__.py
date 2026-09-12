"""Document parsers: raw bytes -> ParsedDocument (text + per-element metadata).

Parsing is intentionally synchronous. Celery's document-processing task
bridges into a single asyncio.run() call per task, and within that call
nothing else is running concurrently on the same loop - so a brief CPU-bound
parse doesn't starve anything the way it would inside a FastAPI request
handler serving multiple concurrent requests.
"""

from app.ingestion.parsers.base import DocumentParser, ParsedDocument, ParsedElement
from app.ingestion.parsers.registry import get_parser_for_mime_type

__all__ = [
    "DocumentParser",
    "ParsedDocument",
    "ParsedElement",
    "get_parser_for_mime_type",
]
