"""Pipeline-internal error taxonomy.

Every failure the ingestion pipeline can hit is classified transient
(worth a Celery retry - a flaky network call, a temporarily-unreachable
LM Studio) or permanent (retrying changes nothing - a corrupt PDF, an
unsupported mime type, a dimension mismatch). The Celery task branches on
this distinction rather than on exception type name matching.
"""


class DocumentProcessingError(Exception):
    """Base class for ingestion pipeline failures."""


class TransientProcessingError(DocumentProcessingError):
    """Retryable: the same input would likely succeed on a later attempt."""


class PermanentProcessingError(DocumentProcessingError):
    """Not retryable: the input itself is the problem."""


class ParsingError(PermanentProcessingError):
    """The document's bytes could not be parsed as its claimed type."""


class EmptyDocumentError(PermanentProcessingError):
    """Parsing succeeded but produced no extractable text."""
