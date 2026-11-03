"""CLI and container-local socket for the host search/read relay."""

import argparse
import json
import socket
import socketserver
import sys

_SOCKET = "/tmp/flashrsi-web.sock"
_MAX_REQUEST = 65536


def _relay():
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            value = self.rfile.readline(_MAX_REQUEST + 1)
            if not value.endswith(b"\n") or len(value) > _MAX_REQUEST:
                self.wfile.write(b'{"error":"Invalid tool request size"}\n')
                return
            sys.stdout.buffer.write(value)
            sys.stdout.buffer.flush()
            reply = sys.stdin.buffer.readline()
            if reply:
                self.wfile.write(reply)

    with socketserver.UnixStreamServer(_SOCKET, Handler) as server:
        print("ready", flush=True)
        server.serve_forever()


def main():
    if sys.argv[1:] == ["--relay"]:
        _relay()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60)
    commands = parser.add_subparsers(dest="action", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--top-k", type=int)
    read = commands.add_parser("read")
    read.add_argument("url")
    step = commands.add_parser("step")
    step.add_argument("text")
    call = commands.add_parser("call")
    call.add_argument("name")
    call.add_argument("arguments", type=json.loads)
    args = parser.parse_args()
    if args.action == "search":
        value = {"action": "search", "query": args.query, "top_k": args.top_k}
    elif args.action == "read":
        value = {"action": "read", "url": args.url}
    elif args.action == "step":
        value = {"action": args.text}
    else:
        value = {"name": args.name, "arguments": args.arguments}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(args.timeout)
        connection.connect(_SOCKET)
        connection.sendall(json.dumps(value).encode() + b"\n")
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline())
    print(json.dumps(result, ensure_ascii=False))
    return int("error" in result)


if __name__ == "__main__":
    raise SystemExit(main())
