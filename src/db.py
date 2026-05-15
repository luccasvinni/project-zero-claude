"""
SQL Server connection — atimo_platform
Used by log_ai.py to log AI token usage.

Credentials can be overridden via environment variables.
"""

import os

import pymssql  # pip install pymssql

DB_HOST     = os.getenv("DB_HOST",     "74.208.183.125")
DB_NAME     = os.getenv("DB_NAME",     "atimo_platform")
DB_USER     = os.getenv("DB_USER",     "Dexter2018")
DB_PASSWORD = os.getenv("DB_PASSWORD", "Roque#331")


def get_conn() -> pymssql.Connection:
    """Return a new SQL Server connection. Caller is responsible for closing."""
    return pymssql.connect(
        server=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="UTF-8",
    )
