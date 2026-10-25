"""Native feed memberships and read-only verification of the legacy ownership ledger."""

import json

import requests

SCHEMA = """CREATE TABLE IF NOT EXISTS managed_collections (
    identity TEXT PRIMARY KEY, rating_key TEXT NOT NULL, guid TEXT NOT NULL
)"""


def entries(response):
    container = response.get("MediaContainer", response)
    return container.get("Metadata", container.get("Directory", [])) or []


def identity(item, library):
    """Episodes of one show must all carry its complete collection list."""
    return (
        library,
        item["kind"],
        item["provider"],
        item["creator_id"] if item["kind"] == "episode" else item["key"],
    )


def memberships(rows, library_name):
    wanted = {}
    for row in rows:
        item = json.loads(row["metadata"])
        names = wanted.setdefault(identity(item, library_name(item)), set())
        if item.get("collection"):
            names.add(item["collection"])
    return {key: sorted(names) for key, names in wanted.items()}


def verify_legacy(request, database):
    """Keep the old ledger for rollback; never edit or delete legacy collections here."""
    with database() as con:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_collections'"
        ).fetchone():
            return
        owned = [dict(row) for row in con.execute("SELECT * FROM managed_collections")]
    for previous in owned:
        try:
            remote = entries(
                request("GET", "/library/collections/" + previous["rating_key"])
            )
        except requests.HTTPError as error:
            if error.response.status_code == 404:
                continue
            raise
        if remote and remote[0].get("guid") != previous["guid"]:
            raise ValueError("Collection GUID changed; refusing to migrate it")
