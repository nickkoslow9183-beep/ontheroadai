"""Request-scoped helpers shared by all dashboard views."""

from __future__ import annotations

import sqlite3

from flask import current_app, flash, g

from ..analytics.csv_import import ImportRejected
from ..db import connect

USER_ERRORS = (ValueError, PermissionError, LookupError, sqlite3.IntegrityError, ImportRejected)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def attempt(fn, success_msg):
    """Run fn; flash success, or flash the error message and roll back."""
    try:
        result = fn()
        if success_msg:
            flash(success_msg if not callable(success_msg) else success_msg(result), "ok")
        return result if result is not None else True
    except USER_ERRORS as exc:
        get_db().rollback()
        flash(str(exc), "error")
        return None
