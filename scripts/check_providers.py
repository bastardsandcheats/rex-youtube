#!/usr/bin/env python3
"""Check any URL or auto-discovered provider fixtures without starting Rex."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import checks, config, reporting, source
from app.provider_registry import catalog, definition_errors, test_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "urls", nargs="*", help="Any public URLs; no provider registration required"
    )
    parser.add_argument(
        "--configured",
        action="store_true",
        help="Test URLs from the active source list, without modifying it",
    )
    parser.add_argument(
        "--config-file", type=Path, help="Alternate YAML settings file for --configured"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Discover every provider file and run its fixtures",
    )
    parser.add_argument(
        "--provider", action="append", help="Only named provider fixtures; repeatable"
    )
    parser.add_argument(
        "--include-live", action="store_true", help="Attempt volatile live fixtures too"
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Attempt experimental fixtures too",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=4,
        help="Decode duration, 1–15 seconds (default 4)",
    )
    parser.add_argument(
        "--timeout", type=float, default=180, help="Whole-case deadline, 10–300 seconds"
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Replace latest.md/json and regenerate PROVIDERS.md here",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Show discovered providers and custom handlers",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of Markdown"
    )
    args = parser.parse_args()
    directory = Path(args.report_dir)
    if args.list:
        reporting.atomic_write(
            directory / "PROVIDERS.md", reporting.provider_markdown()
        )
        print(
            json.dumps(
                {"providers": catalog(), "errors": definition_errors()}, indent=2
            )
            if args.json
            else reporting.provider_markdown()
        )
        return 1 if definition_errors() else 0
    if not args.urls and not args.all and not args.provider and not args.configured:
        parser.error(
            "Supply a URL, --configured, --provider NAME or --all (or use --list)."
        )
    if not 1 <= args.seconds <= 15 or not 10 <= args.timeout <= 300:
        parser.error("seconds must be 1–15 and timeout must be 10–300")
    category = "suite" if args.all else "configured" if args.configured else None
    results = []
    cases = [{"url": url, "kind": None} for url in args.urls]
    if args.configured:
        try:
            settings = (
                config.load_yaml(args.config_file)
                if args.config_file
                else config.load(checks.ROOT)
            )
            configured = config.source_entries(settings)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        if not configured:
            parser.error("The settings file has no enabled sources")
        cases += [
            {"url": target["url"], "kind": None, "settings": target["options"]}
            for entry in configured
            for target in source.configured_targets(entry)
        ]
    if args.all or args.provider:
        try:
            cases += test_cases(
                args.provider, include_live=True, include_experimental=True
            )
        except ValueError as error:
            parser.error(str(error))
        for error in definition_errors():
            if not args.provider or error["provider"] in args.provider:
                results.append(
                    {
                        "provider": error["provider"],
                        "result": "failed",
                        "checks": {
                            "definition": {
                                "status": "failed",
                                "summary": error["message"],
                            }
                        },
                    }
                )
    for number, case in enumerate(cases, 1):
        reason = None
        if case.get("provider"):
            if case["kind"] == "live" and not args.include_live:
                reason = "Live fixture not attempted; use --include-live when the broadcast is online."
            elif case.get("experimental") and not args.include_experimental:
                reason = (
                    "Experimental fixture not attempted; use --include-experimental."
                )
        if reason:
            results.append(dict(case, result="skipped", reason=reason, checks={}))
            continue
        print(
            f"[{number}/{len(cases)}] Checking {case.get('provider', 'URL')} ({case.get('kind') or 'auto'})",
            file=sys.stderr,
            flush=True,
        )
        try:
            result = checks.check_url(
                case["url"],
                seconds=args.seconds,
                timeout=args.timeout,
                expected_kind=case["kind"],
                settings=case.get("settings"),
            )
        except ValueError as error:
            result = {
                "url": checks.public_url(case["url"]),
                "result": "failed",
                "checks": {"request": {"status": "failed", "summary": str(error)}},
            }
        if case.get("provider"):
            result["fixture_provider"] = case["provider"]
            result["fixture_kind"] = case["kind"]
            result.setdefault("provider", case["provider"])
        results.append(result)
        print("  " + result["result"], file=sys.stderr, flush=True)
        # Preserve useful results even if a long suite is interrupted later.
        reporting.write_reports(results, directory, category=category)
    report = reporting.write_reports(results, directory, category=category)
    print(
        json.dumps(report, indent=2, ensure_ascii=False)
        if args.json
        else reporting.markdown(report)
    )
    if any(
        r["result"] in ("failed", "unavailable")
        or any(c["status"] == "failed" for c in r.get("checks", {}).values())
        for r in results
    ):
        return 1
    return 2 if any(r["result"] == "partial" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
