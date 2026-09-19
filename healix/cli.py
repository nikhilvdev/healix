"""The ``healix`` command line.

::

    healix crawl   --config run_config.json --output ./output/ [--webhook-url URL]
    healix extract --manifest ./output/manifest.json [--config run_config.json]

``crawl`` runs discovery then extraction (or discovery only with ``--discover-only``);
``extract`` (re)runs extraction over an existing manifest. Both go through the SDK, so a
run started here emits exactly the events an SDK run does.

Exit status: ``0`` success; ``1`` runtime error; ``2`` usage or configuration error;
``3`` the run finished but some pages failed; ``130`` interrupted (progress is saved, and
the run can be resumed with ``--run-id``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from healix import __version__
from healix.config import ConfigError, RunConfig
from healix.driver.factory import BackendUnavailableError
from healix.log import configure_logging
from healix.sdk import Crawler, Extractor, Run

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_INTERRUPTED = 130

LOG_LEVELS = ("trace", "debug", "info", "warn", "error", "fatal")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healix",
        description="Crawl a website, classify every page, and extract every element to JSON.",
    )
    parser.add_argument("--version", action="version", version=f"healix {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{crawl,extract}")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--webhook-url", help="POST every event to this URL (see HEALIX_WEBHOOK_SECRET)"
        )
        p.add_argument("--headed", action="store_true", help="show the browser window")
        p.add_argument("--json", action="store_true", help="print the run summary as JSON")
        p.add_argument(
            "--log-level", choices=LOG_LEVELS, help="log level on stderr (default: warn)"
        )

    crawl = sub.add_parser("crawl", help="discover every page, then extract each one")
    crawl.add_argument("--config", required=True, help="path to the run config JSON")
    crawl.add_argument("--output", help="output directory (overrides crawl.extraction.output_path)")
    crawl.add_argument("--run-id", help="name the run; reusing an id resumes that run")
    crawl.add_argument(
        "--discover-only", action="store_true", help="find pages but extract nothing"
    )
    common(crawl)

    extract = sub.add_parser("extract", help="extract the pages of an existing manifest")
    extract.add_argument("--manifest", required=True, help="path to manifest.json")
    extract.add_argument(
        "--config", help="path to the run config JSON (default: output beside the manifest)"
    )
    common(extract)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(dotenv_path=Path.cwd() / ".env")  # e.g. HEALIX_WEBHOOK_SECRET
    if args.log_level:
        configure_logging(level=args.log_level)

    runner: Crawler | Extractor | None = None
    try:
        if args.command == "crawl":
            config = RunConfig.load(args.config)
            if args.output:
                config = replace(
                    config, extraction=replace(config.extraction, output_path=args.output)
                )
            runner = Crawler(
                config,
                run_id=args.run_id,
                webhook_url=args.webhook_url,
                headless=not args.headed,
            )
            run = runner.discover() if args.discover_only else runner.discover_and_extract()
        else:
            config_arg = RunConfig.load(args.config, require_start=False) if args.config else None
            runner = Extractor(config_arg, webhook_url=args.webhook_url, headless=not args.headed)
            run = runner.extract(args.manifest)
    except KeyboardInterrupt:
        run_id = getattr(runner, "run_id", None)
        hint = f"; resume with --run-id {run_id}" if run_id else ""
        print(f"healix: interrupted; progress is saved{hint}", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        print(f"healix: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BackendUnavailableError as exc:
        print(f"healix: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"healix: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _report(run, as_json=args.json)
    return EXIT_PARTIAL if run.pages_failed else EXIT_OK


def _report(run: Run, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(run.summary()))
        return
    print(
        f"run {run.run_id}: {run.pages_extracted} of {run.pages_discovered} pages extracted "
        f"(discovery {run.discovery_status})"
    )
    if run.pages_failed:
        print(
            f"  {run.pages_failed} page(s) failed; re-run with --run-id {run.run_id} to retry them"
        )
    print(f"manifest: {run.manifest_path}")


if __name__ == "__main__":
    sys.exit(main())
