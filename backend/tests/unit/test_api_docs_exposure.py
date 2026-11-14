"""Production publishes no API schema; other environments keep the docs."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import api_docs_urls


def _status(environment: str, path: str) -> int:
    app = FastAPI(**api_docs_urls(environment))
    return TestClient(app).get(path).status_code


def test_production_serves_neither_the_schema_nor_the_docs() -> None:
    assert _status("production", "/openapi.json") == 404
    assert _status("production", "/docs") == 404


def test_development_keeps_both() -> None:
    assert _status("development", "/openapi.json") == 200
    assert _status("development", "/docs") == 200
