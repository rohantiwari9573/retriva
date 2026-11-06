"""Deterministic stand-in for an OpenAI-compatible embeddings API.

Used only by local verification and benchmarks (docker-compose.isolated.yml),
so no paid or local model is needed. Standard library only. Vectors are derived
from a hash of the text, so they are repeatable but carry no meaning: results
measure this system's own latency, never embedding quality or a real provider.
"""

import hashlib
import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = int(os.environ.get("STUB_DIMENSIONS", "768"))
DELAY_SECONDS = float(os.environ.get("STUB_DELAY_SECONDS", "0"))


def embed(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(DIMENSIONS)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._send({"status": "ok"})

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["content-length"])))
        time.sleep(DELAY_SECONDS)
        inputs = request["input"] if isinstance(request["input"], list) else [request["input"]]
        data = [{"index": i, "embedding": embed(text)} for i, text in enumerate(inputs)]
        self._send({"data": data})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
