"""Celery application instance.

Kept as a separate module from FastAPI's app so `celery -A app.workers.celery_app`
doesn't import the web app's routing layer.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "nexus",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks.document_processing"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)
