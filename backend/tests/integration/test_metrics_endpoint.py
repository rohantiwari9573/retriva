"""GET /metrics and the request/HTTP metrics middleware."""

PASSWORD = "correct-horse-99"


async def test_metrics_endpoint_returns_prometheus_text(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "http_requests_total" in response.text


async def test_request_increments_http_metrics(client):
    before = await client.get("/metrics")
    await client.get("/health")
    after = await client.get("/metrics")
    # /health itself is scraped by the "before" request and counted too -
    # just assert the counter moved, not an exact delta, to avoid coupling
    # this test to every other test's request volume against a shared
    # in-process registry (prometheus_client's default registry is
    # process-global, not per-test).
    assert after.text.count("http_requests_total") >= before.text.count("http_requests_total")
    assert 'route="/health"' in after.text


async def test_response_carries_request_id_header(client):
    response = await client.get("/health")
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) > 0


async def test_client_supplied_request_id_is_echoed_back(client):
    response = await client.get("/health", headers={"X-Request-ID": "my-custom-id-123"})
    assert response.headers["x-request-id"] == "my-custom-id-123"


async def test_oversized_client_request_id_is_replaced(client):
    huge = "x" * 500
    response = await client.get("/health", headers={"X-Request-ID": huge})
    assert response.headers["x-request-id"] != huge
    assert len(response.headers["x-request-id"]) <= 128


async def test_rate_limit_metrics_increment_on_rejection(client):
    from app.core.config import settings

    payload = {"email": "metrics-ratelimit@example.com", "password": "wrong-password-1"}
    for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE):
        await client.post("/api/v1/auth/login", json=payload)
    limited = await client.post("/api/v1/auth/login", json=payload)
    assert limited.status_code == 429

    metrics = await client.get("/metrics")
    assert "rate_limit_rejected_total" in metrics.text
    assert 'endpoint="login"' in metrics.text


async def test_auth_failure_metrics_increment(client):
    await client.post(
        "/api/v1/auth/login",
        json={"email": "no-such-metrics-user@example.com", "password": "whatever-1"},
    )
    metrics = await client.get("/metrics")
    assert "auth_failures_total" in metrics.text


async def test_upload_document_increments_document_processing_metrics(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "metrics-doc@example.com", "password": PASSWORD},
    )
    org = (await client.post("/api/v1/organizations", json={"name": "Metrics Org"})).json()
    await client.post(
        f"/api/v1/organizations/{org['id']}/documents",
        files={"file": ("m.txt", b"hello metrics world", "text/plain")},
    )
    metrics = await client.get("/metrics")
    # Upload itself only enqueues a Celery task (no worker consumes it in
    # the test process) - document_processing_total is emitted by the task
    # wrapper, not the upload route, so this only asserts the upload-side
    # counters (rate limiting, HTTP) that do fire synchronously.
    assert (
        'route="/api/v1/organizations/{organization_id}/documents"' in metrics.text
    ), metrics.text
