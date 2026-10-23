"""Discover site files without a central list; keep malformed additions isolated."""

import functools
import importlib
import inspect
import pkgutil

HOOKS = (
    "prepare",
    "resolve",
    "enrich",
    "creator",
    "collection_url",
    "targets",
    "live_status",
    "live_url",
    "entry_url",
)


def validate(module, filename):
    """Validate the small declarative contract before a provider enters the runtime."""
    key = getattr(module, "KEY", filename)
    if not isinstance(key, str) or not key:
        raise ValueError("KEY must be a nonempty string")
    if not isinstance(getattr(module, "NAME", None), str) or not module.NAME:
        raise ValueError("NAME must be a nonempty string")
    cases = getattr(module, "TEST_URLS", None)
    if not isinstance(cases, (tuple, list)) or not cases:
        raise ValueError("TEST_URLS must contain at least one case")
    for case in cases:
        if (
            not isinstance(case, dict)
            or case.get("kind") not in ("video", "collection", "live")
            or not str(case.get("url", "")).startswith(("https://", "http://"))
        ):
            raise ValueError(
                "Each TEST_URLS entry needs an HTTP(S) url and video/collection/live kind"
            )
    if not inspect.getdoc(module):
        raise ValueError(
            "Add a module docstring explaining standard or custom provider behavior"
        )
    prefixes = getattr(module, "PREFIXES", (key,))
    if not isinstance(prefixes, (tuple, list)) or any(
        not isinstance(p, str) or not p for p in prefixes
    ):
        raise ValueError("PREFIXES must be a tuple/list of nonempty extractor prefixes")
    for hook in HOOKS:
        if hasattr(module, hook):
            if not callable(getattr(module, hook)):
                raise ValueError(f"{hook} must be callable")
            if not inspect.getdoc(getattr(module, hook)):
                raise ValueError(
                    f"{hook} needs a docstring explaining its custom behavior"
                )
    return key


@functools.lru_cache(maxsize=1)
def discover():
    """Every non-dunder Python module in providers/ is a site, including new files."""
    import providers

    valid, errors = {}, []
    for candidate in sorted(
        pkgutil.iter_modules(providers.__path__), key=lambda m: m.name
    ):
        if candidate.name.startswith("__"):
            continue
        try:
            module = importlib.import_module(f"providers.{candidate.name}")
        except Exception as error:
            errors.append(
                {
                    "provider": candidate.name,
                    "message": f"Provider import failed ({type(error).__name__})",
                }
            )
            continue
        try:
            key = validate(module, candidate.name)
            if key in valid:
                raise ValueError("Duplicate provider KEY")
            valid[key] = module
        except ValueError as error:
            errors.append({"provider": candidate.name, "message": str(error)})
    return valid, errors


def definitions():
    """Return valid site modules; malformed additions are reported separately."""
    return discover()[0]


def definition_errors():
    """Return isolated import/schema errors for the generated report and API."""
    return discover()[1]


def get_definition(key):
    """Find one optional provider module by its normalized key."""
    return definitions().get(key)


def identify(extractor):
    """Match the longest declared extractor prefix, with a standard yt-dlp fallback."""
    name = (extractor or "generic").lower()
    matches = [
        (prefix, key)
        for key, module in definitions().items()
        for prefix in getattr(module, "PREFIXES", (key,))
        if name.startswith(prefix.lower())
    ]
    return (
        max(matches, key=lambda entry: len(entry[0]))[1]
        if matches
        else name.split(":")[0]
    )


def catalog():
    """Public, generated inventory: fixtures plus only the hooks each site supplies."""
    return [
        {
            "key": key,
            "name": module.NAME,
            "file": "providers/" + module.__name__.rsplit(".", 1)[-1] + ".py",
            "description": inspect.getdoc(module) or "",
            "handlers": {
                hook: inspect.getdoc(getattr(module, hook)) or "See provider source."
                for hook in HOOKS
                if hasattr(module, hook)
            },
            "tests": list(module.TEST_URLS),
        }
        for key, module in definitions().items()
    ]


def test_cases(keys=None, *, include_live=False, include_experimental=False):
    """Collect fixtures from discovered sites, optionally including volatile cases."""
    available = definitions()
    unknown = (
        set(keys or ()) - set(available) - {e["provider"] for e in definition_errors()}
    )
    if unknown:
        raise ValueError("Unknown provider: " + ", ".join(sorted(unknown)))
    return [
        dict(case, provider=key)
        for key, module in available.items()
        if not keys or key in keys
        for case in module.TEST_URLS
        if (include_live or case["kind"] != "live")
        and (include_experimental or not case.get("experimental"))
    ]
