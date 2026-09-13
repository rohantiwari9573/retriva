"""Phase 8 Step 40: telemetry must not leak secrets or customer content.

Exercises a real login (so a password and cookies genuinely exist in this
request cycle) and a real document upload, then asserts none of the
sensitive values involved ever appear in /metrics output or in a captured
log line."""

import structlog

PASSWORD = "correct-horse-99-super-secret"


async def test_metrics_endpoint_never_contains_password_or_tokens(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "obs-sec@example.com", "password": PASSWORD},
    )
    access_cookie = client.cookies.get("nexus_access_token")
    refresh_cookie = client.cookies.get("nexus_refresh_token")
    assert access_cookie and refresh_cookie

    metrics = (await client.get("/metrics")).text

    assert PASSWORD not in metrics
    assert access_cookie not in metrics
    assert refresh_cookie not in metrics
    assert "Authorization" not in metrics
    assert "Bearer" not in metrics


async def test_metrics_endpoint_never_contains_document_content(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "obs-sec2@example.com", "password": PASSWORD},
    )
    org = (await client.post("/api/v1/organizations", json={"name": "Secret Org"})).json()
    secret_content = b"CONFIDENTIAL-PROJECT-CODENAME-NIGHTINGALE-DO-NOT-LEAK"
    await client.post(
        f"/api/v1/organizations/{org['id']}/documents",
        files={"file": ("secret.txt", secret_content, "text/plain")},
    )
    metrics = (await client.get("/metrics")).text
    assert "NIGHTINGALE" not in metrics
    assert org["id"] not in metrics
    assert "Secret Org" not in metrics


async def test_metrics_endpoint_never_contains_storage_credentials(client):
    from app.core.config import settings

    metrics = (await client.get("/metrics")).text
    assert settings.S3_SECRET_KEY not in metrics
    assert settings.JWT_SECRET not in metrics
    assert settings.DATABASE_URL not in metrics


async def test_log_line_for_chat_does_not_contain_raw_question_text(client, db_session):
    """chat_completed/chat_stream_completed log only latency/counts (see
    app/services/rag_service.py) - never the question or answer text. Uses
    structlog's testing capture rather than /metrics, since this is a log
    claim, not a metrics claim."""
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "obs-sec3@example.com", "password": PASSWORD},
        )
        org = (await client.post("/api/v1/organizations", json={"name": "Log Org"})).json()
        secret_question = "what is my SUPER-SECRET-QUESTION-MARKER about"
        await client.post(
            f"/api/v1/organizations/{org['id']}/chat", json={"message": secret_question}
        )
        for entry in cap.entries:
            assert "SUPER-SECRET-QUESTION-MARKER" not in str(entry)
    finally:
        from app.core.logging import configure_logging

        configure_logging()
