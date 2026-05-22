"""Shared Postgres connection helper.

The webapp opens one connection per request. Postgres lives on server
(localhost) so connect overhead is sub-ms; pooling is not worth the
complexity in v1.
"""
from __future__ import annotations

import os

import psycopg

PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")


def connect() -> psycopg.Connection:
    return psycopg.connect(PG_DSN)
