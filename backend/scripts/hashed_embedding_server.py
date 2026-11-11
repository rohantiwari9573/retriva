"""Free, offline stand-in for an OpenAI-compatible embeddings API, for evaluation runs.

Each text becomes a hashed bag of its words, so texts sharing words are similar. That's
enough to compare how retrieval strategies and indexes behave on the same vectors, but
it understands no meaning: results say nothing about real embedding models or answer
quality. Standard library only.
"""

import hashlib
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = int(os.environ.get("STUB_DIMENSIONS", "768"))
_WORD = re.compile(r"[a-z0-9]+")


def embed(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSIONS
        vector[index] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


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
        inputs = request["input"] if isinstance(request["input"], list) else [request["input"]]
        self._send({"data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(inputs)]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
