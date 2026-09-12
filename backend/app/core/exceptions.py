"""Centralized exception types and handlers.

All domain errors should raise a subclass of AppError rather than raw HTTPException,
so every error response goes through the same {"error": {"code", "message"}} envelope
and internal details never leak to clients.
"""

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "APP_ERROR"

    def __init__(self, message: str, code: str | None = None, status_code: int | None = None):
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "FILE_TOO_LARGE"


class UnsupportedFileTypeError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "UNSUPPORTED_FILE_TYPE"


class StorageError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "STORAGE_ERROR"


class ProcessingQueueError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "PROCESSING_QUEUE_UNAVAILABLE"


class LLMUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "LLM_UNAVAILABLE"


class LLMTimeoutError(AppError):
    """The LLM backend didn't respond within the configured timeout, before
    producing any output - distinct from LLMUnavailableError (backend
    refused/unreachable) so a client (or the SSE error event, in the
    streaming path) can tell "try again, it might just be slow" apart from
    "something is actually broken"."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "LLM_TIMEOUT"


class EmbeddingUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "EMBEDDING_UNAVAILABLE"


class LLMStreamInterruptedError(AppError):
    """A streaming generation had already produced output when it broke
    (connection lost, malformed chunk) - distinct from LLMUnavailableError,
    which means the backend was never reached at all. Only ever surfaced as
    an SSE `error` event (see app/rag/streaming_events.py), never as a JSON
    response - by the time this can occur, the stream has already started
    and the normal HTTP-error-response path is no longer available."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "LLM_STREAM_INTERRUPTED"


class RetrievalFailedError(AppError):
    """Retrieval failed for a reason other than the embedding backend being
    unavailable (e.g. the database was unreachable mid-query). Deliberately
    distinct from EmbeddingUnavailableError and from the "insufficient
    evidence" answer - those are two different system states that must
    never be conflated: one is a real failure, the other is a normal answer
    meaning "retrieval worked but found nothing relevant"."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "RETRIEVAL_FAILED"


class StorageObjectNotFoundError(StorageError):
    """The storage key doesn't resolve to an object. Distinct from a generic
    StorageError because it's a permanent condition (the row's storage_key is
    stale/gone) - retrying won't fix it, unlike a transient network/5xx error."""

    code = "STORAGE_OBJECT_NOT_FOUND"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request data.",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred.",
                }
            },
        )
