"""Scripted OpenAI-compatible server for tests: no GPU, no real model."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def start_fake(script):
    """Serve /v1/models and /v1/chat/completions, replying with `script` items in order.

    Each item is the assistant `message` to return (the last one repeats), or
    {"status": 400, "body": "..."} for an HTTP error. Returns (url, requests, stop);
    `requests` collects every chat request body.
    """
    requests, state = [], {"i": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                self._send(200, {"object": "list", "data": [{"id": "fake", "object": "model"}]})
            else:
                self._send(200, {"status": "ok"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not self.path.startswith("/v1/chat/completions"):
                return self._send(404, {"error": "not found"})
            requests.append(body)
            item = script[min(state["i"], len(script) - 1)] if script else {"role": "assistant", "content": ""}
            state["i"] += 1
            if "status" in item:
                return self._send(item["status"], item.get("body", ""))
            text = item.get("content") or ""
            self._send(200, {"choices": [{"message": item, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 100, "completion_tokens": max(1, len(text) // 4)}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()

    return f"http://127.0.0.1:{srv.server_address[1]}", requests, stop
