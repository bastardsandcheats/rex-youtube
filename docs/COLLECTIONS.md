# Collections through the Rex feed

Requires Rex with native feed collection support (upstream `7e064697` or newer).
The addon sends collection names inside media feed items. It does not create
collections or add members through the separate Plex collection endpoints.

## Configure names

Keep channel uploads as episodes and group related channel shows:

```yaml
sites:
  youtube:
    sources:
      - url: https://www.youtube.com/@Computerphile
        type: show
        collection: Computing
      - url: https://www.youtube.com/@Numberphile
        type: show
        collection: Learning
```

To group a channel’s individual uploads as movies, use `type: collection` and
`collection: Computing`. `collection_type: movie` is the default; `show` keeps
the episode presentation. `collection: true` uses the channel name, and `false`
disables grouping. Live items use `live_collection`, which defaults to the site
name. See [settings](SETTINGS.md#rex-collections) for inheritance and examples.

## What gets sent

A publication is `POST /rex/feed/{source}/items`. The following is the collection
portion of a movie item; normal placement, display and media-info fields are
also included by the addon:

```json
{
  "items": [
    {
      "id": "stable-video-id",
      "kind": "movie",
      "library": "library-key",
      "collections": ["Computing"]
    }
  ]
}
```

Rex creates or finds the named collection inside that item’s library and adds
the movie. Collections cannot span libraries. For `kind: "episode"`, Rex adds
the parent show rather than the individual episode.

Every episode belonging to one show carries the same complete list of names.
If cached episodes of that show request both Learning and Computing, each
published episode contains:

```json
"collections": ["Computing", "Learning"]
```

This matters because Rex applies the latest supplied list to the parent show.
Sending a different single name on each episode would make the last episode
replace the earlier membership. The addon computes the union from its cached
catalog, scoped to the show, provider and library, before building each batch.

## Clearing and retaining memberships

Rex distinguishes three payloads:

| Field | Meaning |
| --- | --- |
| `"collections": ["Computing"]` | Set this feed source’s requested memberships. |
| `"collections": []` | Clear this source’s requested memberships. |
| Field omitted or `null` | Leave existing memberships unchanged. |

The addon always sends an array, including `[]` when grouping is disabled.
Rex tracks feed-owned membership so other sources’ memberships are retained.
It also preserves a user’s manual membership changes when the source’s
requested list has not changed. Disabling grouping does not delete media;
an empty collection may remain.

Both incremental upserts and complete
`PUT /rex/feed/{source}/snapshot` chunks use the same collection payload builder.
A failed scan keeps cached inventory; absence from an incomplete listing does
not imply removal.

## Upgrading from the previous collection implementation

On first start after upgrading, previously published cached items are queued
once for republication, keeping their feed IDs, file labels and episode numbers.
This reuses saved metadata without running extraction or probing again.

The old `managed_collections` ownership ledger is retained for rollback. Before
the first native publication, the addon verifies the recorded collection GUIDs
using read-only requests. A mismatch stops publication for investigation. The
old per-item collection lookup and create/add/remove reconciliation loop is no
longer used. Existing collection artwork remains managed by Rex.
