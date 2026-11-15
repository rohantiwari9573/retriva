"""Processing lease, recovery sweep, duplicate delivery and bounded concurrency.

Like test_document_processing_task.py, these commit for real (fresh NullPool
engines), because run_attempt() uses its own engine in its own event loop and can
only see committed rows; each test cleans up its own organizations.
"""

import asyncio
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.ingestion import lease, runner
from app.ingestion.pipeline import PipelineOutcome, process_document_pipeline
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.enums import DocumentStatus
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.testing import DeterministicTestEmbeddingProvider
from app.storage.memory import InMemoryStorageProvider

CONTENT = (
    b"First paragraph about the leave policy.\n\n"
    b"Second paragraph about expenses and travel.\n\n"
    b"Third paragraph about security training."
)


@asynccontextmanager
async def _fresh_session():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


@asynccontextmanager
async def _factory():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


async def _make_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with _fresh_session() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
        session.add_all([org, user])
        await session.commit()
        return org.id, user.id


async def _make_document(
    storage: InMemoryStorageProvider,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    content: bytes = CONTENT,
    **lease_state,
) -> uuid.UUID:
    async with _fresh_session() as session:
        document = Document(
            organization_id=org_id,
            uploaded_by=user_id,
            original_filename="doc.txt",
            storage_key=f"pending-{uuid.uuid4().hex}",
            mime_type="text/plain",
            size_bytes=len(content),
            content_hash=uuid.uuid4().hex,
            status=DocumentStatus.PROCESSING,
            processing_heartbeat_at=datetime.now(UTC),
            **lease_state,
        )
        session.add(document)
        await session.flush()
        document.storage_key = f"organizations/{org_id}/documents/{document.id}.txt"
        await storage.upload(document.storage_key, content, "text/plain")
        await session.commit()
        return document.id


async def _fetch(document_id: uuid.UUID) -> Document:
    async with _fresh_session() as session:
        return (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one()


async def _chunks(document_id: uuid.UUID) -> list[DocumentChunk]:
    async with _fresh_session() as session:
        return list(
            (
                await session.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
            ).scalars()
        )


@pytest.fixture
async def org(fake_storage):
    org_id, user_id = await _make_org()
    yield org_id, user_id
    async with _fresh_session() as session:
        ids = (
            (await session.execute(select(Document.id).where(Document.organization_id == org_id)))
            .scalars()
            .all()
        )
        if ids:
            await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(ids)))
        await session.execute(delete(Document).where(Document.organization_id == org_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.fixture
def providers(fake_storage, monkeypatch):
    embedding = DeterministicTestEmbeddingProvider(dimensions=settings.EMBEDDING_DIMENSIONS)
    monkeypatch.setattr(runner, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(runner, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS", 0)
    return embedding


class _SlowProvider(DeterministicTestEmbeddingProvider):
    """Records how many embedding calls are in flight at once."""

    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    async def embed_documents(self, texts):
        with _SlowProvider.lock:
            _SlowProvider.in_flight += 1
            _SlowProvider.max_in_flight = max(_SlowProvider.max_in_flight, _SlowProvider.in_flight)
        try:
            await asyncio.sleep(0.4)
            return await super().embed_documents(texts)
        finally:
            with _SlowProvider.lock:
                _SlowProvider.in_flight -= 1


async def test_duplicate_delivery_processes_once_and_writes_one_set_of_chunks(
    fake_storage, org, monkeypatch
):
    """Two deliveries of the same task at the same time (acks_late redelivery,
    or a sweep re-queue racing the original): one claims, the other skips."""
    slow = _SlowProvider(dimensions=settings.EMBEDDING_DIMENSIONS)
    monkeypatch.setattr(runner, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(runner, "get_embedding_provider", lambda: slow)
    document_id = await _make_document(fake_storage, *org)

    results = await asyncio.gather(
        asyncio.to_thread(runner.run_attempt, str(document_id)),
        asyncio.to_thread(runner.run_attempt, str(document_id)),
    )

    kinds = sorted(r.kind for r in results)
    assert kinds == [runner.AttemptKind.DONE, runner.AttemptKind.SKIPPED]
    document = await _fetch(document_id)
    chunks = await _chunks(document_id)
    assert document.status == DocumentStatus.READY
    assert len(chunks) == document.chunk_count
    assert sorted(c.chunk_index for c in chunks) == list(range(document.chunk_count))
    assert document.processing_attempts == 1
    assert document.processing_token is None


async def test_an_abandoned_lease_is_reclaimed(fake_storage, org, providers):
    """A worker killed mid-way (OOM, restart) leaves a token with an old heartbeat."""
    document_id = await _make_document(
        fake_storage,
        *org,
        processing_token=uuid.uuid4(),
        processing_attempts=1,
    )
    async with _fresh_session() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(processing_heartbeat_at=func.now() - timedelta(hours=1))
        )
        await session.commit()

    result = await asyncio.to_thread(runner.run_attempt, str(document_id))

    assert result.kind == runner.AttemptKind.DONE
    document = await _fetch(document_id)
    assert document.status == DocumentStatus.READY
    assert document.processing_attempts == 2


async def test_a_live_lease_is_never_stolen(fake_storage, org, providers):
    token = uuid.uuid4()
    document_id = await _make_document(fake_storage, *org, processing_token=token)

    result = await asyncio.to_thread(runner.run_attempt, str(document_id))

    assert result.kind == runner.AttemptKind.SKIPPED
    document = await _fetch(document_id)
    assert document.status == DocumentStatus.PROCESSING
    assert document.processing_token == token


async def test_a_document_that_keeps_dying_is_failed_not_retried_forever(
    fake_storage, org, providers
):
    """Each crash costs one attempt; past the limit the next claim fails it."""
    document_id = await _make_document(
        fake_storage,
        *org,
        processing_token=uuid.uuid4(),
        processing_attempts=lease.max_attempts(),
    )
    async with _fresh_session() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(processing_heartbeat_at=func.now() - timedelta(hours=1))
        )
        await session.commit()

    result = await asyncio.to_thread(runner.run_attempt, str(document_id))

    assert result.kind == runner.AttemptKind.FAILED
    document = await _fetch(document_id)
    assert document.status == DocumentStatus.FAILED
    assert document.failure_reason == lease.INTERRUPTED_REASON
    assert await _chunks(document_id) == []


async def test_a_worker_that_lost_its_lease_writes_nothing(fake_storage, org):
    """Lease expired and another worker took over while this one was embedding:
    the stale worker's finish is refused and leaves no chunks."""
    document_id = await _make_document(fake_storage, *org)

    class _TakeoverProvider(DeterministicTestEmbeddingProvider):
        async def embed_documents(self, texts):
            async with _fresh_session() as session:
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(processing_token=uuid.uuid4(), processing_heartbeat_at=func.now())
                )
                await session.commit()
            return await super().embed_documents(texts)

    async with _factory() as factory:
        outcome = await process_document_pipeline(
            str(document_id),
            session_factory=factory,
            storage=fake_storage,
            embedding_provider=_TakeoverProvider(dimensions=settings.EMBEDDING_DIMENSIONS),
        )

    assert outcome == PipelineOutcome.SUPERSEDED
    assert await _chunks(document_id) == []
    assert (await _fetch(document_id)).status == DocumentStatus.PROCESSING


async def test_sweep_requeues_stale_and_abandoned_documents_and_fails_exhausted_ones(
    fake_storage, org, monkeypatch
):
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_LEASE_SECONDS", 60)
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS", 120)
    old = func.now() - timedelta(hours=1)
    fresh_queued = await _make_document(fake_storage, *org)
    stale_queued = await _make_document(fake_storage, *org)
    abandoned = await _make_document(fake_storage, *org, processing_token=uuid.uuid4())
    live = await _make_document(fake_storage, *org, processing_token=uuid.uuid4())
    legacy = await _make_document(fake_storage, *org)  # stranded before the lease columns
    exhausted = await _make_document(
        fake_storage, *org, processing_token=uuid.uuid4(), processing_attempts=lease.max_attempts()
    )
    excluded = await _make_document(fake_storage, *org)
    async with _fresh_session() as session:
        for doc_id in (stale_queued, abandoned, exhausted, excluded):
            await session.execute(
                update(Document).where(Document.id == doc_id).values(processing_heartbeat_at=old)
            )
        await session.execute(
            update(Document).where(Document.id == legacy).values(processing_heartbeat_at=None)
        )
        await session.commit()

    async with _factory() as factory:
        result = await lease.sweep(factory, exclude={str(excluded)})

    mine = {fresh_queued, stale_queued, abandoned, live, legacy, exhausted, excluded}
    assert set(result.requeued) & mine == {stale_queued, abandoned, legacy}
    assert set(result.failed) & mine == {exhausted}
    for doc_id in (stale_queued, abandoned, legacy):
        doc = await _fetch(doc_id)
        assert doc.status == DocumentStatus.PROCESSING
        assert doc.processing_token is None
        assert doc.processing_heartbeat_at is not None
    assert (await _fetch(exhausted)).status == DocumentStatus.FAILED
    assert (await _fetch(live)).processing_token is not None
    assert (await _fetch(excluded)).processing_token is None


async def test_concurrent_sweeps_requeue_a_document_once(fake_storage, org, monkeypatch):
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS", 60)
    document_id = await _make_document(fake_storage, *org)
    async with _fresh_session() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(processing_heartbeat_at=func.now() - timedelta(hours=1))
        )
        await session.commit()

    async with _factory() as a, _factory() as b:
        results = await asyncio.gather(lease.sweep(a), lease.sweep(b))

    assert sum(document_id in r.requeued for r in results) == 1


async def test_transient_failure_releases_the_lease_for_a_later_retry(
    fake_storage, org, monkeypatch
):
    from app.rag.embedding.base import EmbeddingProviderUnavailableError

    class _Down(DeterministicTestEmbeddingProvider):
        async def embed_documents(self, texts):
            raise EmbeddingProviderUnavailableError("AI provider down")

    monkeypatch.setattr(runner, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(
        runner, "get_embedding_provider", lambda: _Down(dimensions=settings.EMBEDDING_DIMENSIONS)
    )
    document_id = await _make_document(fake_storage, *org)

    result = await asyncio.to_thread(runner.run_attempt, str(document_id))

    assert result.kind == runner.AttemptKind.RETRY
    assert result.attempts == 1
    document = await _fetch(document_id)
    assert document.status == DocumentStatus.PROCESSING  # queued again, not stuck
    assert document.processing_token is None
    assert document.retry_count == 1
    assert await _chunks(document_id) == []


async def test_temporary_files_are_removed_after_success_and_failure(
    fake_storage, org, providers, monkeypatch, tmp_path
):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ok_id = await _make_document(fake_storage, *org)
    bad_id = await _make_document(fake_storage, *org, content=b"\xff\xfe not utf-8 \x00")

    ok = await asyncio.to_thread(runner.run_attempt, str(ok_id))
    bad = await asyncio.to_thread(runner.run_attempt, str(bad_id))

    assert ok.kind == runner.AttemptKind.DONE
    assert bad.kind == runner.AttemptKind.FAILED
    assert list(tmp_path.iterdir()) == []


async def test_documents_of_different_organizations_processed_concurrently_stay_separate(
    fake_storage, providers
):
    orgs = [await _make_org() for _ in range(2)]
    try:
        docs = []
        for index, (org_id, user_id) in enumerate(orgs):
            content = f"Organization {index} confidential fact number {index}.".encode()
            docs.append(await _make_document(fake_storage, org_id, user_id, content=content))

        results = await asyncio.gather(
            *(asyncio.to_thread(runner.run_attempt, str(d)) for d in docs)
        )

        assert all(r.kind == runner.AttemptKind.DONE for r in results)
        for index, doc_id in enumerate(docs):
            chunks = await _chunks(doc_id)
            assert chunks and all(f"Organization {index}" in c.content for c in chunks)
            other = 1 - index
            assert all(f"Organization {other}" not in c.content for c in chunks)
    finally:
        async with _fresh_session() as session:
            for org_id, _ in orgs:
                ids = (
                    (
                        await session.execute(
                            select(Document.id).where(Document.organization_id == org_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if ids:
                    await session.execute(
                        delete(DocumentChunk).where(DocumentChunk.document_id.in_(ids))
                    )
                await session.execute(delete(Document).where(Document.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
            await session.commit()


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_inline_runner_bounds_concurrent_processing(
    fake_storage, org, monkeypatch, concurrency
):
    slow = _SlowProvider(dimensions=settings.EMBEDDING_DIMENSIONS)
    _SlowProvider.max_in_flight = 0
    monkeypatch.setattr(runner, "get_storage_provider", lambda: fake_storage)
    monkeypatch.setattr(runner, "get_embedding_provider", lambda: slow)
    docs = [await _make_document(fake_storage, *org) for _ in range(5)]
    inline = runner.InlineRunner(concurrency=concurrency)
    try:
        for doc_id in docs:
            inline.schedule(doc_id)
            inline.schedule(doc_id)  # scheduling twice is a no-op
        deadline = time.monotonic() + 60
        while inline.scheduled and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
    finally:
        inline.shutdown()

    assert _SlowProvider.max_in_flight == concurrency
    for doc_id in docs:
        assert (await _fetch(doc_id)).status == DocumentStatus.READY


async def test_startup_sweep_requeues_freshly_queued_documents_except_our_own(
    fake_storage, org, monkeypatch
):
    """After a restart in inline mode, documents that were queued in the dead
    process's memory are re-queued at once, not after the 30-minute queue limit."""
    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_QUEUE_STALE_SECONDS", 1800)
    orphaned = await _make_document(fake_storage, *org)  # queued just now, no claim
    ours = await _make_document(fake_storage, *org)
    claimed = await _make_document(fake_storage, *org, processing_token=uuid.uuid4())

    async with _factory() as factory:
        regular = await lease.sweep(factory, exclude={str(ours)})
        startup = await lease.sweep(factory, exclude={str(ours)}, queue_stale_seconds=0)

    assert orphaned not in regular.requeued
    assert orphaned in startup.requeued
    assert ours not in startup.requeued
    assert claimed not in startup.requeued  # a live lease is still respected


async def test_a_parse_longer_than_the_lease_keeps_its_lease(
    fake_storage, org, providers, monkeypatch
):
    """Reviewer finding: no heartbeat while parsing, so a slow (legitimate) parse
    outlived its lease and the sweep or a redelivered task re-claimed it under a
    live worker. The background heartbeat keeps it held for the whole parse."""
    from app.ingestion import isolated

    monkeypatch.setattr(settings, "DOCUMENT_PROCESSING_LEASE_SECONDS", 4)  # beat every 1 s
    real_parse = isolated.parse_and_chunk
    stolen: list[bool] = []

    async def slow_parse(path, mime_type, workdir):
        await asyncio.sleep(6)  # 1.5x the lease, with no step-boundary heartbeat
        async with _factory() as session_factory:
            stolen.append(await lease.claim(session_factory, document_id) is not None)
        return await real_parse(path, mime_type, workdir)

    monkeypatch.setattr(isolated, "parse_and_chunk", slow_parse)
    document_id = await _make_document(fake_storage, *org)

    result = await asyncio.to_thread(runner.run_attempt, str(document_id))

    assert stolen == [False]
    assert result.kind == runner.AttemptKind.DONE
    document = await _fetch(document_id)
    assert document.status == DocumentStatus.READY
    assert document.processing_attempts == 1
    assert len(await _chunks(document_id)) == document.chunk_count
