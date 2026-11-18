"""Private fixture-only HTTP service. No database, credentials or external URLs."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps
from threading import Lock

from collector import collect

browser_slot = Lock()


class PilotHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if self.path != "/collect":
            self.send_error(404)
            return
        if self.headers.get("Content-Length") != "2" or self.rfile.read(2) != b"{}":
            self.send_error(400)
            return
        if not browser_slot.acquire(blocking=False):
            self.send_error(429)
            return
        try:
            result = dumps(collect()).encode()
        except Exception:
            self.send_error(503)
            return
        finally:
            browser_slot.release()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), PilotHandler).serve_forever()
