"""End-to-end check of a freshly started stack; exits non-zero on any failure.

Register, log in, create an organization, upload a document, wait until it's
READY, then retrieve it. Used by CI's fresh-start job and TESTING.md. LOCAL
only: it creates data, so never point it at a deployed service.

    python -m scripts.smoke_test --base-url http://localhost:8000
"""

import argparse
import asyncio
import sys
import time
import uuid

import httpx

PASSWORD = "Smoke-Test-Passw0rd!"


async def wait_for_readiness(client: httpx.AsyncClient, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            response = await client.get("/readiness")
            if response.status_code == 200 and response.json()["status"] == "ready":
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("API did not become ready in time")
        await asyncio.sleep(1)


async def run(base_url: str, timeout_seconds: float) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        await wait_for_readiness(client, timeout_seconds)
        print("readiness ok")

        credentials = {"email": f"smoke-{uuid.uuid4().hex[:12]}@example.com", "password": PASSWORD}
        response = await client.post(
            "/api/v1/auth/register", json={**credentials, "full_name": "Smoke Test"}
        )
        response.raise_for_status()
        client.cookies.clear()

        response = await client.post("/api/v1/auth/login", json=credentials)
        response.raise_for_status()
        print("register and login ok")

        response = await client.post(
            "/api/v1/organizations", json={"name": f"Smoke {uuid.uuid4().hex[:8]}"}
        )
        response.raise_for_status()
        org_url = f"/api/v1/organizations/{response.json()['id']}"

        marker = uuid.uuid4().hex
        body = f"The rollback policy for release {marker} is documented here.".encode()
        response = await client.post(
            f"{org_url}/documents", files={"file": ("smoke.txt", body, "text/plain")}
        )
        response.raise_for_status()
        document_url = f"{org_url}/documents/{response.json()['id']}"

        deadline = time.monotonic() + timeout_seconds
        while True:
            document = (await client.get(document_url)).json()
            if document["status"] == "READY":
                break
            if document["status"] == "FAILED":
                raise RuntimeError(f"Ingestion failed: {document.get('failure_reason')}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Document stuck in {document['status']}")
            await asyncio.sleep(0.5)
        print(f"ingestion ok ({document['chunk_count']} chunks)")

        response = await client.post(f"{org_url}/retrieval/debug", json={"query": marker})
        response.raise_for_status()
        print("retrieval ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.base_url, args.timeout))
    except Exception as exc:  # noqa: BLE001 - report any failure as a non-zero exit
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
