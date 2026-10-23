"""Comment-friendly YAML settings, validated before any scanning or publication.

The runtime receives the same small flat settings dictionary used by shared
components, plus normalized source entries. Secrets stay in the private file;
provider definitions and check reports never copy the Rex token.
"""

from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

DEFAULT_OPTIONS = {
    "type": "auto",
    "year_seasons": True,
    "season": 1,
    "max_items": 1000,
    "interval_seconds": 0,
    "check_live": False,
    "max_height": 1080,
    "batch_size": 5,
    "probe": "headers",
    "refresh_hours": 24,
    "collection": False,
    "collection_type": "movie",
    "live_collection": True,
}
ENTRY_KEYS = set(DEFAULT_OPTIONS) | {"url", "name", "enabled"}
SITE_KEYS = set(DEFAULT_OPTIONS) | {
    "enabled",
    "library",
    "movie_library",
    "live_library",
    "sources",
}


def validate_url(url):
    """Accept bounded HTTP(S) page URLs without embedded account credentials."""
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("url must be an HTTP(S) URL of at most 4096 characters")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Use an HTTP(S) URL without embedded credentials")
    return url


class SettingsLoader(yaml.SafeLoader):
    """Duplicate keys usually mean an accidentally hidden site or setting."""


def unique_mapping(loader, node, deep=False):
    """Reject duplicate YAML keys instead of silently losing a site or source."""
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)):
            raise ValueError("Settings keys must be scalars")
        if key in result:
            raise ValueError(f"Duplicate settings key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


SettingsLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def mapping(value, label, allowed):
    """Validate a named settings section and reject misspelled keys."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(label + " must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            label
            + " has unknown setting(s): "
            + ", ".join(map(str, sorted(unknown, key=str)))
        )
    return value


def boolean(value, label):
    """Require actual YAML booleans, not quoted strings."""
    if not isinstance(value, bool):
        raise ValueError(label + " must be true or false")
    return value


def integer(value, label, minimum, maximum):
    """Validate numeric settings without accepting booleans as integers."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def options(values, label):
    """Validate inherited source options once, keeping typos out of the scanner."""
    if values["type"] not in ("auto", "show", "movie", "live", "collection"):
        raise ValueError(label + ".type must be auto, show, movie, live or collection")
    if not isinstance(values["probe"], bool) and values["probe"] != "headers":
        raise ValueError(label + ".probe must be true, false or headers")
    if values["collection_type"] not in ("movie", "show"):
        raise ValueError(label + ".collection_type must be movie or show")
    for field in ("collection", "live_collection"):
        value = values[field]
        if not isinstance(value, bool) and (
            not isinstance(value, str) or not value.strip() or len(value) > 500
        ):
            raise ValueError(
                label + "." + field + " must be true, false or a collection name"
            )
    for field in ("year_seasons", "check_live"):
        boolean(values[field], label + "." + field)
    for field, low, high in [
        ("refresh_hours", 1, 8760),
        ("season", 0, 9999),
        ("max_items", 0, 100000),
        ("interval_seconds", 0, 86400),
        ("max_height", 144, 4320),
        ("batch_size", 1, 1000),
    ]:
        integer(values[field], label + "." + field, low, high)
    if values.get("name") is not None and (
        not isinstance(values["name"], str) or len(values["name"]) > 500
    ):
        raise ValueError(label + ".name must be a string of at most 500 characters")
    return values


def normalize(document):
    """Resolve global, site and source inheritance into validated runtime settings."""
    document = mapping(
        document, "settings", {"server", "rex", "scan", "playback", "defaults", "sites"}
    )
    server = mapping(
        document.get("server"), "server", {"host", "port", "public_url", "log_level"}
    )
    rex = mapping(document.get("rex"), "rex", {"url", "token", "source_id"})
    scan = mapping(
        document.get("scan"),
        "scan",
        {"interval_seconds", "batch_size", "max_items", "snapshot_hours"},
    )
    playback = mapping(
        document.get("playback"), "playback", {"max_height", "live_buffer_mb"}
    )
    defaults = mapping(document.get("defaults"), "defaults", set(DEFAULT_OPTIONS))
    sites = document.get("sites", {})
    if not isinstance(sites, dict):
        raise ValueError("sites must be a mapping keyed by provider name")
    url = rex.get("url", "http://REX_HOST:REX_PORT")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("rex.url must be an HTTP(S) URL")
    level = str(server.get("log_level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("server.log_level is invalid")
    cfg = {
        "rex_url": url,
        "rex_token": rex.get("token") or "",
        "source": rex.get("source_id", "youtube"),
        "host": server.get("host", "0.0.0.0"),
        "port": integer(server.get("port", 8790), "server.port", 1, 65535),
        "source_url": server.get("public_url") or "",
        "log_level": level,
        "interval_seconds": integer(
            scan.get("interval_seconds", 900), "scan.interval_seconds", 1, 86400
        ),
        "snapshot_hours": integer(
            scan.get("snapshot_hours", 6), "scan.snapshot_hours", 1, 8760
        ),
        "batch_size": integer(scan.get("batch_size", 5), "scan.batch_size", 1, 1000),
        "collection_limit": integer(
            scan.get("max_items", 1000), "scan.max_items", 0, 100000
        ),
        "max_height": integer(
            playback.get("max_height", 1080), "playback.max_height", 144, 4320
        ),
        "live_buffer_mb": integer(
            playback.get("live_buffer_mb", 16), "playback.live_buffer_mb", 1, 64
        ),
        "site_settings": {},
        "source_entries": [],
    }
    for key in ("rex_token", "source", "host", "source_url"):
        if not isinstance(cfg[key], str):
            raise ValueError(key + " must be a string")
    common = options(
        {
            **DEFAULT_OPTIONS,
            "max_items": cfg["collection_limit"],
            "batch_size": cfg["batch_size"],
            "max_height": cfg["max_height"],
            **defaults,
        },
        "defaults",
    )
    for site, raw in sites.items():
        if not isinstance(site, str) or not re.fullmatch(r"[a-z0-9_-]+", site):
            raise ValueError(
                "Site keys must use lowercase letters, numbers, hyphens or underscores"
            )
        raw = mapping(raw, "sites." + site, SITE_KEYS)
        enabled = boolean(raw.get("enabled", True), "sites." + site + ".enabled")
        for field in ("library", "movie_library", "live_library"):
            if field in raw and (
                not isinstance(raw[field], str) or not raw[field].strip()
            ):
                raise ValueError(
                    "sites." + site + "." + field + " must be a nonempty string"
                )
        inherited = options(
            {**common, **{k: raw[k] for k in DEFAULT_OPTIONS if k in raw}},
            "sites." + site,
        )
        cfg["site_settings"][site] = raw
        entries = raw.get("sources", [])
        if not isinstance(entries, list):
            raise ValueError("sites." + site + ".sources must be a list")
        for index, entry in enumerate(entries):
            label = f"sites.{site}.sources[{index}]"
            entry = mapping(entry, label, ENTRY_KEYS)
            validate_url(entry.get("url"))
            entry_enabled = boolean(entry.get("enabled", True), label + ".enabled")
            merged = options(
                {
                    **inherited,
                    **{
                        k: entry[k]
                        for k in ENTRY_KEYS - {"enabled", "url"}
                        if k in entry
                    },
                },
                label,
            )
            if merged["type"] == "live":
                merged["check_live"] = True
            merged.update(
                site=site,
                name=merged.get("name") or "",
                library=raw.get("library"),
                movie_library=raw.get("movie_library"),
                live_library=raw.get("live_library"),
            )
            if enabled and entry_enabled:
                cfg["source_entries"].append({"url": entry["url"], "options": merged})
    return cfg


def load(root):
    """Load the single active YAML file; no alternate settings formats are supported."""
    root = Path(root)
    candidates = [root / "config" / name for name in ("config.yml", "config.yaml")]
    present = [p for p in candidates if p.exists()]
    if len(present) > 1:
        raise ValueError("Keep one settings file: config.yml or config.yaml, not both")
    if present:
        return load_yaml(present[0])
    raise ValueError("Create config/config.yml from config/config.example.yml")


def load_yaml(path):
    """Read explicit YAML without echoing parser snippets containing secrets."""
    try:
        document = yaml.load(Path(path).read_text(), Loader=SettingsLoader)
    except yaml.YAMLError as error:
        raise ValueError(
            "Invalid YAML; check indentation and quoted values in the settings file"
        ) from error
    return normalize(document)


def source_entries(cfg):
    """Return enabled, validated YAML entries shared by scanning and checks."""
    return cfg["source_entries"]
