from app.ingestion.errors import PermanentProcessingError
from app.ingestion.parsers.base import DocumentParser
from app.ingestion.parsers.docx import DOCXParser
from app.ingestion.parsers.markdown import MarkdownParser
from app.ingestion.parsers.pdf import PDFParser
from app.ingestion.parsers.txt import TXTParser

_PARSERS_BY_MIME: dict[str, type[DocumentParser]] = {
    "application/pdf": PDFParser,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXParser,
    "text/plain": TXTParser,
    "text/markdown": MarkdownParser,
}


def get_parser_for_mime_type(mime_type: str) -> DocumentParser:
    parser_cls = _PARSERS_BY_MIME.get(mime_type)
    if parser_cls is None:
        raise PermanentProcessingError(f"No parser registered for mime type '{mime_type}'.")
    return parser_cls()
