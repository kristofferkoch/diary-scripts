#!/usr/bin/env python3
"""Send the persistent mail-web login link to Signal note-to-self.

The token is stored in a caller-owned ``0600`` file outside the workspace. The
login URL carries it only in the fragment, so web servers never log it. No
token or login link is printed to stdout/stderr.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from mail_reader.auth import AuthError, token_for_notification
from mail_reader.config import mail_web_url
from scripts.dsh_web_notify import signal_note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--rotate", action="store_true", help="replace the persistent token before sending")
    args = parser.parse_args(argv)
    try:
        token = token_for_notification(rotate=args.rotate)
        url = mail_web_url() + "/login#token=" + quote(token, safe="")
        signal_note(f"mail reader login link:\n{url}")
    except (AuthError, OSError, RuntimeError) as exc:
        print(f"mail-reader-token-notify: {exc}", file=sys.stderr)
        return 1
    print("mail-reader-token-notify: sent login link to Signal note-to-self")
    return 0


if __name__ == "__main__":
    sys.exit(main())
