"""Aggregates all v1 route modules under a single router.

New route modules (auth, users, organizations, documents, ...) get included here as
they're built in later phases, keeping app.main free of per-feature imports.
"""

from fastapi import APIRouter

api_router = APIRouter()

# Phase 2+: api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
