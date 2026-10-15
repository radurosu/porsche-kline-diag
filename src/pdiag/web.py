"""Local web UI: stdlib HTTP server, JSON API, Server-Sent Events for live data.

JSON: /api/status, /api/ident, /api/dtcs, /api/over-rev.
Streams: /api/live (decoded OBD readings), /api/dme-live (raw DME data blocks).
Only requests addressed to 127.0.0.1 or localhost are answered (DNS rebinding).
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .service import Car, NotDecoded
from .transport import KLineError

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
         ".svg": "image/svg+xml"}


def make_handler(car: Car):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _json(self, code: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api(self, fn) -> None:
            try:
                self._json(200, fn())
            except NotDecoded as e:
                self._json(501, {"error": str(e), "kind": "not_decoded"})
            except KLineError as e:
                self._json(502, {"error": str(e), "kind": "kline"})
            except Exception as e:  # keep the UI informed instead of dropping the socket
                self._json(500, {"error": f"{type(e).__name__}: {e}", "kind": "internal"})

        def do_GET(self):
            # Only answer pages addressed to us. A site using DNS rebinding sends
            # its own name as Host; without this it could read the VIN.
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._json(403, {"error": "forbidden host"})
            path = self.path.split("?")[0]
            routes = {"/api/status": car.status, "/api/dtcs": car.dtcs,
                      "/api/ident": car.ident, "/api/over-rev": car.over_rev}
            if path in routes:
                return self._api(routes[path])
            if path == "/api/live":
                return self._stream(car.live, lambda v: {"values": v})
            if path == "/api/dme-live":
                return self._stream(car.dme_live, lambda v: v)
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if f.resolve().parent != STATIC.resolve() or not f.is_file():
                return self._json(404, {"error": "not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, read, shape):
            """Server-Sent Events: one message per read, until the page goes away."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    t0 = time.perf_counter()
                    try:
                        msg = {"t": time.time(), **shape(read())}
                    except NotDecoded as e:
                        msg = {"error": str(e), "kind": "not_decoded"}
                    except KLineError as e:
                        msg = {"error": str(e), "kind": "kline"}
                    except Exception as e:  # a bug must not silently kill the stream
                        msg = {"error": f"{type(e).__name__}: {e}", "kind": "internal"}
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
                    if "error" in msg:
                        time.sleep(2)
                    time.sleep(max(0, 0.1 - (time.perf_counter() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(car: Car, host: str = "127.0.0.1", port: int = 8996, open_browser: bool = True):
    httpd = ThreadingHTTPServer((host, port), make_handler(car))
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"pdiag UI on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
