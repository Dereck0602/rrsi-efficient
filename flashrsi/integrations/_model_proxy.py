"""HTTP proxy for the configured model origins, run in a separate container."""

import http.client
import json
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _headers(headers):
    excluded = _HOP_HEADERS | {
        item.strip().lower()
        for value in headers.get_all("Connection", [])
        for item in value.split(",")
    }
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


def _handler(allowed):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(300)

        def log_message(self, *_):
            pass

        def _destination(self, value, scheme):
            try:
                parsed = urlsplit(value)
                port = parsed.port or (443 if scheme == "https" else 80)
                origin = (scheme, parsed.hostname, port)
                if (
                    parsed.scheme == scheme
                    and not (parsed.username or parsed.password or parsed.fragment)
                    and origin in allowed
                ):
                    return parsed, port
            except ValueError:
                pass
            self.send_error(403, "Destination is outside the configured model origins")
            return None

        def do_CONNECT(self):
            destination = self._destination("https://" + self.path, "https")
            if destination is None:
                return
            parsed, port = destination
            if parsed.path or parsed.query:
                self.send_error(400, "Invalid CONNECT destination")
                return
            try:
                upstream = socket.create_connection((parsed.hostname, port), timeout=30)
            except OSError:
                self.send_error(502, "Model endpoint unavailable")
                return
            with upstream:
                self.send_response_only(200, "Connection established")
                self.end_headers()
                peers = {self.connection: upstream, upstream: self.connection}
                try:
                    while ready := select.select(list(peers), [], [], 300)[0]:
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            peers[source].sendall(data)
                except OSError:
                    return

        def _forward_http(self):
            destination = self._destination(self.path, "http")
            if destination is None:
                return
            parsed, port = destination
            try:
                size = int(self.headers.get("Content-Length", 0))
                if (
                    self.headers.get("Transfer-Encoding")
                    or not 0 <= size <= 64 * 1024 * 1024
                ):
                    raise ValueError
            except ValueError:
                self.send_error(400, "Invalid model request body")
                return
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=300)
            try:
                path = (parsed.path or "/") + (
                    "?" + parsed.query if parsed.query else ""
                )
                headers = _headers(self.headers)
                # The actual configured origin owns the request Host header.
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() != "host"
                }
                connection.request(
                    self.command, path, body=self.rfile.read(size), headers=headers
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                connection.close()
                self.send_error(502, "Model endpoint unavailable")
                return
            try:
                self.send_response_only(response.status)
                for key, value in _headers(response.headers).items():
                    self.send_header(key, value)
                self.end_headers()
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()

        do_GET = do_POST = _forward_http

    return Handler


if __name__ == "__main__":
    allowed = {tuple(item) for item in json.loads(sys.argv[1])}
    with ThreadingHTTPServer(("model-egress", 8080), _handler(allowed)) as server:
        server.serve_forever()
