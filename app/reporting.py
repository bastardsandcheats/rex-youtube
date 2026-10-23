"""Generate readable reports and the provider inventory from actual definitions."""

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from app.provider_registry import catalog, definition_errors

_LOCK = threading.Lock()


def cell(value):
    """Escape table delimiters and newlines for generated Markdown."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def provider_markdown():
    """The handler table is generated, so adding a hook cannot silently hide it."""
    lines = [
        "# Provider inventory (generated)",
        "",
        "Generated from `providers/*.py`. Every non-dunder site file is discovered automatically. Shared runtime and registry code live in `app/`.",
        "",
        "| Provider | Custom handlers | Source |",
        "| --- | --- | --- |",
    ]
    for site in catalog():
        handlers = (
            ", ".join("`" + name + "`" for name in site["handlers"])
            or "None: standard yt-dlp output"
        )
        lines.append(
            f"| {cell(site['name'])} | {handlers} | [{site['file']}](../{site['file']}) |"
        )
    lines += [
        "",
        "## Handler map",
        "",
        "```mermaid",
        "flowchart LR",
        '  shared["Shared extraction<br/>Metadata and HTTP relay"]',
    ]
    for number, site in enumerate(catalog()):
        label = site["name"].replace('"', "&quot;")
        # Keep generated nodes narrow enough to read in GitHub's document column.
        handlers = "<br/>".join(site["handlers"]) or "standard yt-dlp output"
        lines.append(f'  site{number}["{label}<br/>{handlers}"] --> shared')
    lines += [
        "```",
        "",
        "Hooks are optional lifecycle callbacks, not separate playback engines. The map lists only the handlers actually defined by each site file.",
    ]
    for site in catalog():
        lines += ["", "## " + site["name"], "", site["description"], ""]
        for name, description in site["handlers"].items():
            lines.append("- `" + name + "`: " + description)
        lines += ["", "| Test | URL |", "| --- | --- |"]
        for test in site["tests"]:
            kind = test["kind"] + (
                " (experimental)" if test.get("experimental") else ""
            )
            lines.append(f"| {kind} | {cell(test['url'])} |")
    if definition_errors():
        lines += ["", "## Invalid definitions", ""]
        lines += [
            "- " + error["provider"] + ": " + error["message"]
            for error in definition_errors()
        ]
    return "\n".join(lines) + "\n"


def suite(results):
    """Attach one run identity, timestamp and outcome summary."""
    counts = Counter(r["result"] for r in results)
    return {
        "schema_version": 1,
        "report_id": uuid.uuid4().hex,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Extractor, metadata and real HTTP relay decoding; no Rex or client invoked.",
        "summary": dict(counts),
        "results": results,
    }


def markdown(report):
    """Render the capability overview and per-stage details."""
    lines = [
        "# Provider check report",
        "",
        "Generated: " + report["generated_at"],
        "",
        report["scope"],
        "",
        "Summary: "
        + ", ".join(f"{key}={count}" for key, count in report["summary"].items()),
        "",
    ]
    lines += [
        "| Provider | Case | Result | Playback | Metadata | Seek |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        capabilities = result.get("capabilities", {})
        checks = result.get("checks", {})
        label = result.get("provider") or result.get("fixture_provider") or "URL"
        kind = result.get("fixture_kind") or result.get("kind") or "auto"
        playback = "decoded" if capabilities.get("playable") else "not verified"
        metadata = checks.get("metadata", {}).get("status", "skipped")
        seek = checks.get("seek", {}).get("status", "skipped")
        lines.append(
            f"| {cell(label)} | {kind} | {result['result']} | {playback} | {metadata} | {seek} |"
        )
    lines.append("")
    for result in report["results"]:
        lines += [
            "## "
            + cell(result.get("provider", "Unregistered URL"))
            + " — "
            + result["result"],
            "",
            result.get("url", ""),
            "",
        ]
        if result.get("reason"):
            lines += [result["reason"], ""]
        if result.get("item"):
            item = result["item"]
            lines += [
                "Title: " + cell(item["title"]),
                "",
                "Creator: " + cell(item["creator_name"]),
                "",
                "Kind: "
                + item["kind"]
                + "; publication date: "
                + (item["published"] or "missing"),
                "",
            ]
        lines += ["| Check | Result | Details |", "| --- | --- | --- |"]
        for name, check in result.get("checks", {}).items():
            detail = check["summary"]
            for field in ("missing", "mismatches"):
                if check.get(field):
                    detail += " " + ", ".join(check[field])
            if "frames" in check:
                detail += (
                    f" {check['frames']} frames, {check['seconds_decoded']}s decoded."
                )
            lines.append(f"| {name} | {check['status']} | {cell(detail)} |")
        if result.get("capabilities"):
            lines += [
                "",
                "Capabilities: "
                + ", ".join(
                    f"{name}={str(value).lower()}"
                    for name, value in result["capabilities"].items()
                ),
                "",
            ]
    lines += [
        "",
        "A collection check samples one representative video. A short decode or seek sample is not a full-length playback or client-compatibility guarantee.",
        "",
    ]
    return "\n".join(lines)


def atomic_write(path, text):
    """Reports replace the previous run; avoid accumulating a diagnostics directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".__report-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_reports(results, directory, category=None):
    """Replace canonical reports and regenerate the site/handler inventory."""
    directory = Path(directory)
    report = suite(results)
    with _LOCK:
        atomic_write(
            directory / "latest.json",
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
        atomic_write(directory / "latest.md", markdown(report))
        if category in ("suite", "configured"):
            atomic_write(
                directory / (category + ".json"),
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            )
            atomic_write(directory / (category + ".md"), markdown(report))
        atomic_write(directory / "PROVIDERS.md", provider_markdown())
    return report
