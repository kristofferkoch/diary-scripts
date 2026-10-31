"""Download one Signal attachment through the restricted signal-notify proxy."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
from pathlib import Path

from scripts.signal_notify_proxy import DEFAULT_LISTEN, MAX_ATTACHMENT_BYTES

MAX_RESPONSE_BYTES = (MAX_ATTACHMENT_BYTES + 2) // 3 * 4 + 1_000_000


def fetch_attachment(attachment_id: str, socket_path: str, timeout: float = 30.0) -> bytes:
    request = {"jsonrpc": "2.0", "id": 1, "method": "getAttachment",
               "params": {"id": attachment_id}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("signal proxy closed the connection without a reply")
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise RuntimeError("signal proxy response too large")
    response = json.loads(buf.split(b"\n", 1)[0])
    if response.get("id") != 1:
        raise RuntimeError("signal proxy returned a mismatched response id")
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "signal proxy error"))
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), str):
        raise RuntimeError("signal proxy returned an invalid attachment response")
    return base64.b64decode(result["data"], validate=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attachment_id", help="attachment id from signal-capture JSONL")
    parser.add_argument("--output", required=True, help="new output file, or - for stdout")
    parser.add_argument("--socket", default=os.environ.get("SIGNAL_NOTIFY_SOCKET", DEFAULT_LISTEN))
    args = parser.parse_args(argv)
    try:
        data = fetch_attachment(args.attachment_id, args.socket)
        if args.output == "-":
            sys.stdout.buffer.write(data)
        else:
            with Path(args.output).open("xb") as output:
                output.write(data)
            print(f"wrote {args.output} ({len(data)} bytes)", file=sys.stderr)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"signal-attachment: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
