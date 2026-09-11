"""
Connecting to Neon.

pg8000 is a pure-Python driver and comes from the separate neon-db-driver
layer, not this one - this module only knows how to call it. Both layers are
attached to any function that talks to the database.

pg8000 takes discrete keyword arguments rather than a postgres:// DSN, so the
connection string is parsed and passed through with an explicit SSL context.
Neon requires TLS.
"""

import ssl
import urllib.parse

import pg8000.dbapi

SSL_CONTEXT = ssl.create_default_context()


def connect(conn_string):
    parsed = urllib.parse.urlparse(conn_string)
    return pg8000.dbapi.connect(
        user=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=(parsed.path or "/").lstrip("/"),
        ssl_context=SSL_CONTEXT,
    )
