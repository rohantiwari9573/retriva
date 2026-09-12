"""Aggregates all v1 route modules under a single router."""

from fastapi import APIRouter

from app.api.v1 import auth, organizations, users

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(organizations.router, prefix="/organizations", tags=["organizations"])
