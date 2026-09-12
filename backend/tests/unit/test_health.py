async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness(client):
    response = await client.get("/liveness")
    assert response.status_code == 200
