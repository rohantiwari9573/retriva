"""Unit tests for the SSE event protocol - pure formatting, no HTTP/DB."""

import json
import uuid

from app.rag.citations import Citation
from app.rag.streaming_events import (
    CitationsEvent,
    ErrorEvent,
    MessageCompleteEvent,
    MessageStartEvent,
    TokenEvent,
    format_sse,
)


def _parse(frame: str) -> tuple[str, dict]:
    lines = frame.strip("\n").split("\n")
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    event_name = lines[0][len("event: ") :]
    data = json.loads(lines[1][len("data: ") :])
    return event_name, data


def test_message_start_event_shape():
    conv_id, msg_id = uuid.uuid4(), uuid.uuid4()
    frame = format_sse(MessageStartEvent(conversation_id=conv_id, user_message_id=msg_id))

    name, data = _parse(frame)
    assert name == "message_start"
    assert data == {"conversation_id": str(conv_id), "user_message_id": str(msg_id)}


def test_token_event_shape():
    name, data = _parse(format_sse(TokenEvent(text="Hello")))
    assert name == "token"
    assert data == {"text": "Hello"}


def test_citations_event_shape_with_empty_list():
    name, data = _parse(format_sse(CitationsEvent(citations=[])))
    assert name == "citations"
    assert data == {"citations": []}


def test_citations_event_shape_with_citations():
    citation = Citation(
        id="SOURCE-1",
        document_id=str(uuid.uuid4()),
        document_name="handbook.txt",
        chunk_id=str(uuid.uuid4()),
        page=14,
        section="Leave Policy",
        excerpt="Employees receive 24 days...",
    )
    name, data = _parse(format_sse(CitationsEvent(citations=[citation])))
    assert name == "citations"
    assert data["citations"][0]["document_name"] == "handbook.txt"
    assert data["citations"][0]["page"] == 14


def test_message_complete_event_shape():
    msg_id = uuid.uuid4()
    frame = format_sse(
        MessageCompleteEvent(
            message_id=msg_id, answer="The answer.", chunks_considered=3, chunks_used=1
        )
    )
    name, data = _parse(frame)
    assert name == "message_complete"
    assert data == {
        "message_id": str(msg_id),
        "answer": "The answer.",
        "chunks_considered": 3,
        "chunks_used": 1,
    }


def test_error_event_shape():
    name, data = _parse(format_sse(ErrorEvent(code="LLM_UNAVAILABLE", message="down")))
    assert name == "error"
    assert data == {"code": "LLM_UNAVAILABLE", "message": "down"}


def test_frame_ends_with_blank_line_per_sse_spec():
    frame = format_sse(TokenEvent(text="x"))
    assert frame.endswith("\n\n")
