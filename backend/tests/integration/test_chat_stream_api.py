"""Route-level tests for POST /{organization_id}/chat/stream - the SSE
counterpart to /chat (see tests/integration/test_chat_api.py for the
non-streaming route, which already covers most auth/RBAC/tenant-isolation
cases this deliberately doesn't re-litigate). Uses httpx's ASGITransport
streaming support (client.stream(...).aiter_lines()) - no real server."""

import json
import uuid

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider

PASSWORD = "correct-horse-99"
EMBED = DeterministicTestEmbeddingProvider(dimensions=768)
CHUNK_CONTENT = "Employees receive 24 days of annual leave per year."


async def _register_and_create_org(client, email: str, org_name: str):
    await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    org = (await client.post("/api/v1/organizations", json={"name": org_name})).json()
    return org


async def _login(client, email: str):
    client.cookies.clear()
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})


async def _seed_chunk(db_session, org_id: str, *, content: str = CHUNK_CONTENT) -> DocumentChunk:
    document = Document(
        organization_id=uuid.UUID(org_id),
        original_filename="handbook.txt",
        storage_key=f"key-{uuid.uuid4()}",
        mime_type="text/plain",
        size_bytes=len(content),
        content_hash=uuid.uuid4().hex,
        status=DocumentStatus.READY,
    )
    db_session.add(document)
    await db_session.flush()

    vector = await EMBED.embed_query(content)
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        content=content,
        page_number=14,
        section="Leave Policy",
        char_count=len(content),
        token_count=12,
        content_hash=uuid.uuid4().hex,
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.commit()
    return chunk


async def _collect_sse_events(client, url: str, payload: dict) -> list[tuple[str, dict]]:
    """Send a streaming POST and return the parsed (event_name, data) pairs,
    in arrival order - the one helper every test in this file uses so the
    SSE-frame-parsing logic exists in exactly one place."""
    events: list[tuple[str, dict]] = []
    async with client.stream("POST", url, json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        event_name = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                assert event_name is not None
                events.append((event_name, json.loads(line[len("data: ") :])))
    return events


async def test_stream_returns_full_event_sequence_with_citation(client, db_session):
    org = await _register_and_create_org(client, "stream-owner1@example.com", "Stream Org A")
    await _seed_chunk(db_session, org["id"])

    events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/chat/stream",
        {"message": CHUNK_CONTENT},
    )

    names = [name for name, _ in events]
    assert names[0] == "message_start"
    assert "token" in names
    assert names[-2:] == ["citations", "message_complete"]

    citations_data = next(data for name, data in events if name == "citations")
    assert len(citations_data["citations"]) == 1
    assert citations_data["citations"][0]["document_name"] == "handbook.txt"

    complete_data = next(data for name, data in events if name == "message_complete")
    assert "[SOURCE-1]" in complete_data["answer"]


async def test_stream_with_no_documents_returns_insufficient_evidence(client):
    org = await _register_and_create_org(client, "stream-owner2@example.com", "Stream Org B")

    events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/chat/stream",
        {"message": "Anything at all?"},
    )

    names = [name for name, _ in events]
    assert "token" not in names  # no evidence -> no LLM call at all
    complete_data = next(data for name, data in events if name == "message_complete")
    assert "couldn't find enough information" in complete_data["answer"]


async def test_stream_persists_conversation_visible_via_non_streaming_api(client, db_session):
    org = await _register_and_create_org(client, "stream-owner3@example.com", "Stream Org C")
    await _seed_chunk(db_session, org["id"])

    events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/chat/stream",
        {"message": CHUNK_CONTENT},
    )
    conversation_id = next(data for name, data in events if name == "message_start")[
        "conversation_id"
    ]

    detail_response = await client.get(
        f"/api/v1/organizations/{org['id']}/conversations/{conversation_id}"
    )
    assert detail_response.status_code == 200
    messages = detail_response.json()["messages"]
    assert len(messages) == 2
    assert messages[1]["citations"][0]["document_name"] == "handbook.txt"


async def test_cross_tenant_cannot_stream_about_another_orgs_documents(client, db_session):
    org_a = await _register_and_create_org(client, "stream-a-owner@example.com", "Stream Alpha")
    await _seed_chunk(db_session, org_a["id"])

    await _register_and_create_org(client, "stream-b-owner@example.com", "Stream Beta")
    await _login(client, "stream-b-owner@example.com")

    response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/chat/stream", json={"message": CHUNK_CONTENT}
    )
    assert response.status_code == 404


async def test_cross_tenant_conversation_id_returns_404_before_streaming_starts(
    client, db_session
):
    org_a = await _register_and_create_org(client, "stream-a-owner2@example.com", "Stream Alpha 2")
    await _seed_chunk(db_session, org_a["id"])
    chat_response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/chat", json={"message": CHUNK_CONTENT}
    )
    other_conversation_id = chat_response.json()["conversation_id"]

    org_b = await _register_and_create_org(client, "stream-b-owner2@example.com", "Stream Beta 2")

    # A real 404 JSON response, not an SSE error event - the cross-tenant
    # conversation id is rejected before the stream ever starts.
    response = await client.post(
        f"/api/v1/organizations/{org_b['id']}/chat/stream",
        json={"conversation_id": other_conversation_id, "message": "Tell me more."},
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


async def test_streaming_disabled_returns_503_json_not_a_stream(client, db_session, monkeypatch):
    from app.core.config import settings

    org = await _register_and_create_org(client, "stream-owner6@example.com", "Stream Org F")
    await _seed_chunk(db_session, org["id"])
    monkeypatch.setattr(settings, "STREAMING_ENABLED", False)

    response = await client.post(
        f"/api/v1/organizations/{org['id']}/chat/stream", json={"message": CHUNK_CONTENT}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STREAMING_DISABLED"


async def test_viewer_can_stream_chat(client, db_session):
    org = await _register_and_create_org(client, "stream-owner4@example.com", "Stream Org D")
    await _seed_chunk(db_session, org["id"])
    await client.post(
        "/api/v1/auth/register",
        json={"email": "stream-viewer4@example.com", "password": PASSWORD},
    )
    await _login(client, "stream-owner4@example.com")
    await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"email": "stream-viewer4@example.com", "role": "VIEWER"},
    )

    await _login(client, "stream-viewer4@example.com")
    events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/chat/stream",
        {"message": CHUNK_CONTENT},
    )
    assert events[0][0] == "message_start"


async def test_regenerate_appends_new_assistant_message(client, db_session):
    org = await _register_and_create_org(client, "regen-owner1@example.com", "Regen Org A")
    await _seed_chunk(db_session, org["id"])

    first_events = await _collect_sse_events(
        client, f"/api/v1/organizations/{org['id']}/chat/stream", {"message": CHUNK_CONTENT}
    )
    conversation_id = next(data for name, data in first_events if name == "message_start")[
        "conversation_id"
    ]
    first_message_id = next(
        data for name, data in first_events if name == "message_complete"
    )["message_id"]

    regen_events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/conversations/{conversation_id}/regenerate",
        {},
    )
    regen_message_id = next(
        data for name, data in regen_events if name == "message_complete"
    )["message_id"]
    assert regen_message_id != first_message_id

    detail_response = await client.get(
        f"/api/v1/organizations/{org['id']}/conversations/{conversation_id}"
    )
    messages = detail_response.json()["messages"]
    assert [m["role"] for m in messages] == ["USER", "ASSISTANT", "ASSISTANT"]


async def test_regenerate_cross_tenant_conversation_returns_404(client, db_session):
    org_a = await _register_and_create_org(client, "regen-a-owner@example.com", "Regen Alpha")
    await _seed_chunk(db_session, org_a["id"])
    events = await _collect_sse_events(
        client, f"/api/v1/organizations/{org_a['id']}/chat/stream", {"message": CHUNK_CONTENT}
    )
    conversation_id = next(data for name, data in events if name == "message_start")[
        "conversation_id"
    ]

    await _register_and_create_org(client, "regen-b-owner@example.com", "Regen Beta")
    await _login(client, "regen-b-owner@example.com")

    response = await client.post(
        f"/api/v1/organizations/{org_a['id']}/conversations/{conversation_id}/regenerate",
        json={},
    )
    assert response.status_code == 404


async def test_zero_valid_citations_reports_insufficient_evidence_over_stream(
    client, db_session, fake_llm_provider
):
    org = await _register_and_create_org(client, "stream-owner5@example.com", "Stream Org E")
    await _seed_chunk(db_session, org["id"])
    # The client fixture wires this exact instance in as the app's
    # LLMProvider - fixing its response to a fabricated citation tag lets
    # this test actually exercise citation stripping over the real route,
    # not just assert the route returns *some* answer.
    fake_llm_provider.fixed_response = "The answer is X. [SOURCE-99]"

    events = await _collect_sse_events(
        client,
        f"/api/v1/organizations/{org['id']}/chat/stream",
        {"message": CHUNK_CONTENT},
    )

    citations_data = next(data for name, data in events if name == "citations")
    assert citations_data["citations"] == []
    complete_data = next(data for name, data in events if name == "message_complete")
    assert "couldn't find enough information" in complete_data["answer"]
