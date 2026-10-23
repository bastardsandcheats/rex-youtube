"""Static Rex collections, following PlexControl's verified Plex-compatible APIs.

A collection is scoped to one library and member kind. Own only recorded GUIDs;
never adopt a similarly named collection or delete any member's media.
"""

import json
import requests

SCHEMA = """CREATE TABLE IF NOT EXISTS managed_collections (
    identity TEXT PRIMARY KEY, rating_key TEXT NOT NULL, guid TEXT NOT NULL
)"""


def entries(response):
    container = response.get("MediaContainer", response)
    return container.get("Metadata", container.get("Directory", [])) or []


def reconcile(rows, request, database, *, prune=True):
    """Reconcile only this addon's collections; callers supply its active feed rows."""
    wanted = {}
    metadata = {}
    for row in rows:
        item = json.loads(row["metadata"])
        title = item.get("collection")
        if not title or not row.get("rating_key") or row["status"] != "published":
            continue
        identity_key = (
            item["provider"],
            item.get("library_name"),
            item["kind"],
            item["creator_id"] if item["kind"] == "episode" else row["rating_key"],
        )
        if identity_key not in metadata:
            metadata[identity_key] = entries(
                request("GET", "/library/metadata/" + row["rating_key"])
            )
        meta = metadata[identity_key]
        if not meta:
            raise ValueError("Collection member disappeared from Rex")
        meta = meta[0]
        section = str(meta["librarySectionID"])
        episode = item["kind"] == "episode"
        key = str(meta["grandparentRatingKey"] if episode else meta["ratingKey"])
        kind = 2 if episode else 1
        identity = json.dumps([section, kind, title])
        group = wanted.setdefault(
            identity, dict(section=section, kind=kind, title=title, keys=set())
        )
        group["keys"].add(key)
    with database() as con:
        con.execute(SCHEMA)
        owned = {
            r["identity"]: dict(r)
            for r in con.execute("SELECT * FROM managed_collections")
        }
    if not wanted and not owned:
        return
    machine = request("GET", "/identity").get("MediaContainer", {})
    machine = machine.get("machineIdentifier")
    if not machine:
        raise ValueError("Rex identity has no machineIdentifier")
    for identity in wanted.keys() | owned.keys():
        previous = owned.get(identity)
        remote = None
        if previous:
            try:
                remote = entries(
                    request("GET", "/library/collections/" + previous["rating_key"])
                )
            except requests.HTTPError as error:
                if error.response.status_code != 404:
                    raise
            if remote and remote[0].get("guid") != previous["guid"]:
                raise ValueError("Collection GUID changed; refusing to modify it")
        if identity not in wanted:
            if not prune:
                continue
            if remote:
                request("DELETE", "/library/collections/" + previous["rating_key"])
            with database() as con:
                con.execute(
                    "DELETE FROM managed_collections WHERE identity=?", (identity,)
                )
            continue
        group = wanted[identity]
        if not remote:
            remote = entries(
                request(
                    "POST",
                    "/library/collections",
                    params={
                        "type": group["kind"],
                        "title": group["title"],
                        "smart": 0,
                        "sectionId": group["section"],
                    },
                )
            )
            if (
                not remote
                or not remote[0].get("guid")
                or not remote[0].get("ratingKey")
            ):
                raise ValueError("Rex collection is missing its key or GUID")
            previous = dict(
                rating_key=str(remote[0]["ratingKey"]), guid=remote[0]["guid"]
            )
            with database() as con:
                con.execute(
                    "INSERT OR REPLACE INTO managed_collections VALUES(?,?,?)",
                    (identity, previous["rating_key"], previous["guid"]),
                )
        key = previous["rating_key"]
        current = {
            str(r["ratingKey"])
            for r in entries(request("GET", f"/library/collections/{key}/children"))
        }
        missing = sorted(group["keys"] - current)
        for offset in range(0, len(missing), 100):
            request(
                "PUT",
                f"/library/collections/{key}/items",
                params={
                    "uri": f"server://{machine}/com.plexapp.plugins.library/library/metadata/"
                    + ",".join(missing[offset : offset + 100])
                },
            )
        if prune:
            for member in current - group["keys"]:
                request("DELETE", f"/library/collections/{key}/items/{member}")
