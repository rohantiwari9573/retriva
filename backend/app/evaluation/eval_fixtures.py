"""Idempotent evaluation-corpus setup: a dedicated organization, user, and a
fixed set of real markdown documents (app/evaluation/fixtures/docs/) ingested
through the REAL production code paths - DocumentService.upload() (storage +
DB row, same as a real HTTP upload) and process_document_pipeline() (the
same parse/chunk/embed/persist logic a Celery worker runs), called directly
rather than via Celery's broker so evaluation setup is synchronous and
doesn't depend on a worker process being up.

This is evaluation-only tooling, never imported by the request-serving path
(app/api/, app/services/rag_service.py) - see docs/evaluation.md's "No data
leakage" section. It does not disable authorization or bypass tenant
scoping: it creates one real, ordinary organization and issues real service-
layer calls against it, the same way any other org's documents get ingested.

WHY REAL DOCUMENTATION FILES AS THE FIXTURE CORPUS: the Phase 10 spec
requires "carefully designed questions grounded in actual documents" and
explicitly forbids fabricated expected chunks. This project has no sample
business-document corpus lying around from earlier phases (Phase 5/6 assume
a manually-uploaded "Employee Handbook" that was never checked into the
repo - see app/evaluation/dataset.py's docstring). Using Retriva's own real
project documentation (docs/architecture.md, security.md, retrieval.md,
streaming.md, observability.md, document-ingestion.md, copied verbatim into
fixtures/docs/) gives a genuine, substantial, checked-in corpus with
defensible ground truth an author can actually verify by reading it -
rather than either inventing a fake company handbook or leaving evaluation
unreproducible from a fresh clone.

IDEMPOTENCY: safe to run repeatedly against the same database. The
organization/user are found-or-created by a fixed slug/email; each document
is found-or-created by content hash, reusing DocumentService.upload()'s
existing duplicate-content detection (see docs/security.md) rather than a
separate check - a second run's "duplicate" is simply treated as "already
ingested," not an error.
"""

import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.ingestion.pipeline import process_document_pipeline
from app.models.document import Document
from app.models.enums import DocumentStatus, OrgRole
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.rag.embedding.base import EmbeddingProvider
from app.repositories.document_repository import DocumentRepository
from app.services.document_service import DocumentService
from app.storage.base import StorageProvider

logger = get_logger(__name__)

FIXTURE_ORG_SLUG = "retriva-eval-fixture"
FIXTURE_ORG_NAME = "Retriva Evaluation Fixture"
FIXTURE_USER_EMAIL = "eval-fixture@retriva.local"
FIXTURE_DOCS_DIR = Path(__file__).parent / "fixtures" / "docs"


@dataclass(frozen=True)
class EvalCorpus:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    document_names: tuple[str, ...]


async def _get_or_create_org_and_user(db: AsyncSession) -> tuple[Organization, User]:
    org = (
        await db.execute(select(Organization).where(Organization.slug == FIXTURE_ORG_SLUG))
    ).scalar_one_or_none()
    user = (
        await db.execute(select(User).where(User.email == FIXTURE_USER_EMAIL))
    ).scalar_one_or_none()

    created_something = False
    if org is None:
        org = Organization(name=FIXTURE_ORG_NAME, slug=FIXTURE_ORG_SLUG)
        db.add(org)
        created_something = True
    if user is None:
        # Password is never used for login - this account only exists as an
        # `uploaded_by` foreign key target for evaluation documents, never
        # authenticated against. A random-looking placeholder makes that
        # intent obvious to anyone reading the row rather than implying a
        # real, usable credential exists.
        user = User(
            email=FIXTURE_USER_EMAIL, hashed_password="not-a-real-account:eval-fixture-only"
        )
        db.add(user)
        created_something = True
    await db.flush()

    membership = (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrgRole.OWNER))
        created_something = True

    if not created_something:
        # Nothing to persist - skip the commit/refresh cycle entirely rather
        # than churn a no-op transaction boundary on every idempotent
        # re-run (this function is called at the start of every evaluation
        # run - see ensure_eval_corpus).
        return org, user

    await db.flush()
    await db.commit()
    await db.refresh(org)
    await db.refresh(user)
    return org, user


async def _ingest_fixture_document(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    storage: StorageProvider,
    embedding_provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    filename: str,
    content: bytes,
) -> Document:
    # Plain UUIDs, not ORM objects, by design - see ensure_eval_corpus's
    # comment: rollback() below unconditionally expires every object in the
    # session (unlike commit, there is no expire_on_commit=False equivalent
    # for rollback, since in-memory state genuinely no longer reflects the
    # database afterward). Accepting the ORM `Organization`/`User` objects
    # here and reading `.id` off them after a rollback would trigger an
    # implicit, un-awaited lazy-reload and crash with "MissingGreenlet:
    # greenlet_spawn has not been called" - observed directly while testing
    # this module's duplicate-content path, not a hypothetical concern.
    service = DocumentService(db, storage)
    existing: Document | None
    try:
        document = await service.upload(
            org_id=org_id, uploaded_by=user_id, file=_upload_file(filename, content)
        )
    except (ConflictError, IntegrityError):
        # Already ingested in a previous run (uq_document_org_content_hash -
        # see docs/security.md) - fetch the existing row instead of treating
        # re-running fixture setup as an error.
        await db.rollback()
        existing = await _find_existing(db, org_id, filename)
        if existing is None:
            raise
        document = existing

    if document.status != DocumentStatus.READY:
        # Covers both a fresh upload (status=PROCESSING) and re-running
        # setup against a previously-FAILED fixture document (e.g. a prior
        # run with LM Studio unreachable) - process synchronously (no
        # Celery worker dependency), the same pipeline function a real
        # worker calls, called directly, exactly like
        # tests/integration/test_document_pipeline.py does.
        await process_document_pipeline(
            str(document.id),
            session_factory=session_factory,
            storage=storage,
            embedding_provider=embedding_provider,
        )
        await db.refresh(document)
    return document


async def _find_existing(db: AsyncSession, org_id: uuid.UUID, filename: str) -> Document | None:
    return (
        (
            await db.execute(
                select(Document).where(
                    Document.organization_id == org_id, Document.original_filename == filename
                )
            )
        )
        .scalars()
        .first()
    )


def _upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


async def ensure_eval_corpus(
    db: AsyncSession,
    *,
    storage: StorageProvider,
    embedding_provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> EvalCorpus:
    """Ensures the fixture organization exists and every file in
    fixtures/docs/ is uploaded and READY, then returns their identities.
    Safe to call on every evaluation run - see module docstring."""
    org, user = await _get_or_create_org_and_user(db)
    # Plain values, not the ORM objects - a duplicate-content rollback for
    # fixture file N would otherwise expire `org`/`user` for every
    # subsequent iteration too (rollback expires the whole session
    # regardless of expire_on_commit - see _ingest_fixture_document's
    # comment on the exact crash this avoids).
    org_id, user_id = org.id, user.id

    fixture_files = sorted(FIXTURE_DOCS_DIR.glob("*.md"))
    if not fixture_files:
        raise RuntimeError(
            f"No fixture documents found in {FIXTURE_DOCS_DIR} - the evaluation "
            "corpus cannot be built. See app/evaluation/fixtures/docs/."
        )

    document_names: list[str] = []
    for path in fixture_files:
        content = path.read_bytes()
        document = await _ingest_fixture_document(
            db,
            org_id=org_id,
            user_id=user_id,
            storage=storage,
            embedding_provider=embedding_provider,
            session_factory=session_factory,
            filename=path.name,
            content=content,
        )
        logger.info(
            "eval_fixture_document_ready",
            filename=path.name,
            status=document.status.value,
            chunk_count=document.chunk_count,
        )
        document_names.append(path.name)

    return EvalCorpus(organization_id=org.id, user_id=user.id, document_names=tuple(document_names))


async def corpus_chunk_count(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """A quick sanity-check helper for the CLI/report - not used by
    production code."""
    repo = DocumentRepository(db)
    documents, _ = await repo.list_for_org(organization_id, page=1, page_size=100)
    return sum(d.chunk_count for d in documents)
