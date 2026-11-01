"""Extract only public host endpoints into the fixed root-owned hosts TOML."""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from mail_reader.hosts_config import HOSTS_CONFIG_PATH, read_hosts, render_hosts, validate_hosts
from scripts.safe_io import atomic_write_text

TRUSTED_CONFIG_PATH = Path("/etc/diary/trusted.toml")


def extract_hosts(path: Path | None = None) -> dict[str, str]:
    path = path or TRUSTED_CONFIG_PATH
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("trusted config must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            document = tomllib.load(handle)
    finally:
        os.close(fd)
    if "hosts" not in document:
        raise ValueError("trusted config has no [hosts] table")
    return validate_hosts(document["hosts"])


def install_public_hosts() -> None:
    hosts = extract_hosts()
    atomic_write_text(HOSTS_CONFIG_PATH, render_hosts(hosts), mode=0o644)
    os.chown(HOSTS_CONFIG_PATH, 0, 0)
    os.chmod(HOSTS_CONFIG_PATH, 0o644)
    info = os.stat(HOSTS_CONFIG_PATH)
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o777 != 0o644:
        raise RuntimeError("public hosts config ownership/mode verification failed")
    if read_hosts() != hosts:
        raise RuntimeError("public hosts config content verification failed")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("public hosts installation must run as root")
    install_public_hosts()


if __name__ == "__main__":
    main()
