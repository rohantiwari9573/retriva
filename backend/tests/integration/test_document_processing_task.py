"""Tests for the Celery task wrapper itself (retry/failure classification,
FAILED persistence).

Unlike every other test in this suite, these do NOT use the db_session
fixture. The task always calls asyncio.run() internally (see
document_processing._session_factory's docstring - it needs a connection
whose pool isn't bound to a foreign event loop), which requires running the
task in a thread with no already-running loop (asyncio.to_thread from this
async test), and that thread's fresh engine/connection can only see rows
another connection has actually COMMITTED - not rows sitting inside
db_session's uncommitted SAVEPOINT-nested test transaction. So these tests
use AsyncSessionLocal directly (autocommitting for real, like production)
to set up documents and to read back results, and clean up their own rows
explicitly since nothing will roll them back automatically.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.ingestion.errors import PermanentProcessingError, TransientProcessingError
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.storage.memory import InMemoryStorageProvider
from app.workers.tasks import document_processing


class _AlwaysTransientProvider:
    model = "always-transient"
    dimensions = 768

    async def embed_documents(self, texts):
        raise TransientProcessingError("LM Studio temporarily unreachable")

    async def embed_query(self, text):
        raise TransientProcessingError("LM Studio temporarily unreachable")


class _FailsThenSucceedsProvider:
    model = "flaky"
    dimensions = 768

    def __init__(self, fail_times: int) -> None:
        self._remaining_failures = fail_times
        self._inner = DeterministicTestEmbeddingProvider(dimensions=768)

    async def embed_documents(self, texts):
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TransientProcessingError("transient hiccup")
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text):
        return await self._inner.embed_query(text)


class _AlwaysPermanentProvider:
    model = "always-permanent"
    dimensions = 768

    async def embed_documents(self, texts):
        raise PermanentProcessingError("model rejected input")

    async def embed_query(self, text):
        raise PermanentProcessingError("model rejected input")


@asynccontextmanager
async def _fresh_session():
    # A dedicated NullPool engine per call, exactly like
    # document_processing._session_factory - each pytest-asyncio test gets
    # its own event loop, and a shared/pooled engine (e.g.
    # app.core.database.AsyncSessionLocal) would hand out a connection whose
    # pool is bound to a previous test's already-closed loop.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def _make_document(storage: InMemoryStorageProvider) -> uuid.UUID:
    content = b"Paragraph with real content for the task-level test.\n\nSecond paragraph."
    async with _fresh_session() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
        session.add_all([org, user])
        await session.flush()

        document = Document(
            organization_id=org.id,
            uploaded_by=user.id,
            original_filename="doc.txt",
            storage_key="pending",
            mime_type="text/plain",
            size_bytes=len(content),
            content_hash=uuid.uuid4().hex,
            status=DocumentStatus.PROCESSING,
        )
        session.add(document)
        await session.flush()
        document.storage_key = f"organizations/{org.id}/documents/{document.id}.txt"
        await storage.upload(document.storage_key, content, "text/plain")
        await session.commit()
        return document.id


async def _fetch(document_id: uuid.UUID) -> Document:
    async with _fresh_session() as session:
        return (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one()


async def _run_task(document_id: uuid.UUID) -> None:
    # The task body (including its own asyncio.run()) must run without an
    # already-running event loop in this thread - exactly the environment a
    # real Celery worker process provides, and exactly what this test
    # process (already inside pytest-asyncio's loop) does not.
    await asyncio.to_thread(
        lambda: document_processing.process_document.apply(args=[str(document_id)]).get()
    )


@pytest.fixture(autouse=True)
async def _cleanup(fake_storage):
    created_org_ids: list[uuid.UUID] = []
    yield created_org_ids
    if not created_org_ids:
        return
    async with _fresh_session() as session:
        for org_id in created_org_ids:
            docs = (
                await session.execute(select(Document).where(Document.organization_id == org_id))
            ).scalars().all()
            for doc in docs:
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                )
            await session.execute(delete(Document).where(Document.organization_id == org_id))
            await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


async def test_transient_failure_exhausting_retries_marks_document_failed(
    fake_storage, monkeypatch, _cleanup
):
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_MAX_RETRIES", 1)
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(document_processing, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(
        document_processing, "get_embedding_provider", lambda: _AlwaysTransientProvider()
    )
    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)

    await _run_task(document_id)

    refreshed = await _fetch(document_id)
    assert refreshed.status == DocumentStatus.FAILED
    assert refreshed.failure_reason is not None
    assert refreshed.retry_count >= 1


async def test_transient_failure_then_success_reaches_ready(fake_storage, monkeypatch, _cleanup):
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_MAX_RETRIES", 3)
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(document_processing, "get_storage_provider", lambda: fake_storage)
    # One shared instance across calls - get_embedding_provider() is invoked
    # fresh on every attempt (matching production, which has no notion of
    # "the same provider instance across a retry"), so the fail-then-succeed
    # counter must live outside the factory closure to actually count down.
    flaky_provider = _FailsThenSucceedsProvider(fail_times=1)
    monkeypatch.setattr(
        document_processing, "get_embedding_provider", lambda: flaky_provider
    )
    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)

    await _run_task(document_id)

    refreshed = await _fetch(document_id)
    assert refreshed.status == DocumentStatus.READY
    assert refreshed.chunk_count > 0


async def test_permanent_failure_marks_failed_without_retry_bookkeeping(
    fake_storage, monkeypatch, _cleanup
):
    monkeypatch.setattr(document_processing, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(
        document_processing, "get_embedding_provider", lambda: _AlwaysPermanentProvider()
    )
    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)

    await _run_task(document_id)

    refreshed = await _fetch(document_id)
    assert refreshed.status == DocumentStatus.FAILED
    assert "model rejected input" in refreshed.failure_reason
    assert refreshed.retry_count == 0  # never retried - permanent, not transient


async def test_unexpected_exception_never_leaks_raw_message(fake_storage, monkeypatch, _cleanup):
    def _boom():
        raise RuntimeError("internal secret: /etc/some/path leaked here")

    monkeypatch.setattr(document_processing, "get_storage_provider", _boom)
    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)

    await _run_task(document_id)


def _counter_value(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()  # noqa: SLF001 - test-only introspection


async def test_successful_run_increments_success_metrics(fake_storage, monkeypatch, _cleanup):
    from app.core.metrics import celery_tasks_total, document_processing_total
    from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
    from app.workers.tasks.document_processing import _TASK_NAME

    monkeypatch.setattr(document_processing, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(
        document_processing,
        "get_embedding_provider",
        lambda: DeterministicTestEmbeddingProvider(dimensions=768),
    )
    before = _counter_value(document_processing_total, status="success")
    before_task = _counter_value(celery_tasks_total, task_name=_TASK_NAME, status="success")

    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)
    await _run_task(document_id)

    assert (await _fetch(document_id)).status == DocumentStatus.READY
    assert _counter_value(document_processing_total, status="success") == before + 1
    assert (
        _counter_value(celery_tasks_total, task_name=_TASK_NAME, status="success")
        == before_task + 1
    )


async def test_permanent_failure_increments_failure_metrics(
    fake_storage, monkeypatch, _cleanup
):
    from app.core.metrics import celery_task_failures_total, document_processing_total
    from app.workers.tasks.document_processing import _TASK_NAME

    monkeypatch.setattr(document_processing, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(
        document_processing, "get_embedding_provider", lambda: _AlwaysPermanentProvider()
    )
    before = _counter_value(document_processing_total, status="failure")
    before_task = _counter_value(celery_task_failures_total, task_name=_TASK_NAME)

    document_id = await _make_document(fake_storage)
    _cleanup.append((await _fetch(document_id)).organization_id)
    await _run_task(document_id)

    assert (await _fetch(document_id)).status == DocumentStatus.FAILED
    assert _counter_value(document_processing_total, status="failure") == before + 1
    assert _counter_value(celery_task_failures_total, task_name=_TASK_NAME) == before_task + 1

    refreshed = await _fetch(document_id)
    assert refreshed.status == DocumentStatus.FAILED
    assert "secret" not in refreshed.failure_reason
    assert "/etc/" not in refreshed.failure_reason
