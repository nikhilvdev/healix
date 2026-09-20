"""The ``healix`` command line.

::

    healix crawl    --config run_config.json --output ./output/ [--webhook-url URL]
    healix extract  --manifest ./output/manifest.json [--config run_config.json]
    healix generate --input ./output/manifest.json --backend playwright --style pom
    healix doctor   [--launch]
    healix flush-events --outbox events.db --webhook-url URL

``crawl`` runs discovery then extraction (or discovery only with ``--discover-only``);
``extract`` (re)runs extraction over an existing manifest; ``generate`` writes a self-healing
Playwright or Selenium script from the extracted pages; ``doctor`` checks that this machine can run
Healix; ``flush-events`` sends the webhook events a run left in its ``--webhook-outbox`` because the
receiver was down. All but ``doctor`` and ``flush-events`` go through the SDK, so a command run
here emits exactly the events an SDK call does.

Exit status: ``0`` success; ``1`` runtime error; ``2`` usage or configuration error;
``3`` the run finished but some pages failed; ``4`` blocked on authentication (a login page
was reached but no credentials are set); ``130`` interrupted (progress is saved, and the run
can be resumed with ``--run-id``). ``doctor`` exits ``0`` when Healix has a usable browser backend
and ``1`` when it does not. ``flush-events`` exits ``0`` when the outbox is empty, ``1`` when events
are still waiting (the receiver could not be reached) and ``3`` when the receiver rejected some.

Logging in is automatic: a page that classifies as ``login`` is filled with
``WEBLIB_LOGIN_USERNAME`` / ``WEBLIB_LOGIN_PASSWORD`` (from the environment or a ``.env`` in the
current directory). ``--no-login`` turns that off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from healix import __version__
from healix.auth import PASSWORD_ENV, USERNAME_ENV
from healix.config import ConfigError, RunConfig
from healix.doctor import format_report, run_doctor
from healix.driver.factory import BackendUnavailableError
from healix.events import DurableWebhookSender, Outbox, OutboxError
from healix.events.outbox import DEAD, PENDING
from healix.generation import BACKENDS, STYLES, GenerationError, ScriptGenerator
from healix.log import configure_logging
from healix.sdk import WEBHOOK_SECRET_ENV, Crawler, Extractor, Run

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_BLOCKED = 4
EXIT_INTERRUPTED = 130

LOG_LEVELS = ("trace", "debug", "info", "warn", "error", "fatal")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healix",
        description="Crawl a website, classify every page, and extract every element to JSON.",
    )
    parser.add_argument("--version", action="version", version=f"healix {__version__}")
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{crawl,extract,generate,doctor,flush-events}"
    )

    def reporting(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--webhook-url", help="POST every event to this URL (see HEALIX_WEBHOOK_SECRET)"
        )
        p.add_argument(
            "--webhook-outbox",
            metavar="PATH",
            help="keep every event in this file until the receiver accepts it, and resend what a "
            "down receiver missed (needs --webhook-url; see `healix flush-events`)",
        )
        p.add_argument("--json", action="store_true", help="print the result as JSON")
        p.add_argument(
            "--log-level", choices=LOG_LEVELS, help="log level on stderr (default: warn)"
        )

    def common(p: argparse.ArgumentParser) -> None:
        reporting(p)
        p.add_argument("--headed", action="store_true", help="show the browser window")
        p.add_argument(
            "--no-login", action="store_true", help="do not log in when a login page is reached"
        )
        p.add_argument(
            "--fingerprint-db",
            metavar="PATH",
            help="record element fingerprints for later self-healing: a SQLite file path or a "
            "postgresql:// URL",
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

    generate = sub.add_parser(
        "generate", help="write a self-healing Playwright or Selenium script from extracted pages"
    )
    generate.add_argument("--input", required=True, help="path to manifest.json")
    generate.add_argument("--backend", choices=BACKENDS, default="playwright")
    generate.add_argument(
        "--style",
        choices=STYLES,
        default="pom",
        help="pom: page objects; test: pytest assertions; action: fill and click only",
    )
    generate.add_argument(
        "--output", help="directory to write to (default: <manifest dir>/scripts)"
    )
    generate.add_argument(
        "--fingerprint-db",
        metavar="PATH",
        help="the fingerprint store the script heals against: a SQLite path or a postgresql:// "
        "URL (default: healix.db). Fingerprints are recorded there now, if not already",
    )
    generate.add_argument(
        "--no-record",
        action="store_true",
        help="do not record fingerprints (they were recorded during the crawl)",
    )
    reporting(generate)

    flush = sub.add_parser(
        "flush-events",
        help="send the webhook events still waiting in an outbox",
    )
    flush.add_argument("--outbox", required=True, metavar="PATH", help="the --webhook-outbox file")
    flush.add_argument("--webhook-url", required=True, help="where to send them")
    flush.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how long to keep trying before giving up (default: 60)",
    )
    flush.add_argument(
        "--retry-rejected",
        action="store_true",
        help="also send again the events the receiver rejected earlier",
    )
    flush.add_argument("--json", action="store_true", help="print the result as JSON")
    flush.add_argument(
        "--log-level", choices=LOG_LEVELS, help="log level on stderr (default: warn)"
    )

    doctor = sub.add_parser("doctor", help="check that this machine can run Healix")
    doctor.add_argument(
        "--launch", action="store_true", help="also open each usable browser and read a page"
    )
    doctor.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(dotenv_path=Path.cwd() / ".env")  # e.g. HEALIX_WEBHOOK_SECRET
    if getattr(args, "log_level", None):
        configure_logging(level=args.log_level)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "generate":
        return _generate(args)
    if args.command == "flush-events":
        return _flush_events(args)

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
                webhook_outbox=args.webhook_outbox,
                headless=not args.headed,
                auto_login=not args.no_login,
                fingerprint_store=args.fingerprint_db,
            )
            run = runner.discover() if args.discover_only else runner.discover_and_extract()
        else:
            config_arg = RunConfig.load(args.config, require_start=False) if args.config else None
            runner = Extractor(
                config_arg,
                webhook_url=args.webhook_url,
                webhook_outbox=args.webhook_outbox,
                headless=not args.headed,
                auto_login=not args.no_login,
                fingerprint_store=args.fingerprint_db,
            )
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
    _outbox_note(args.webhook_outbox)
    if run.blocked_on_auth:
        return EXIT_BLOCKED
    return EXIT_PARTIAL if run.pages_failed else EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    report = run_doctor(launch=args.launch)
    print(json.dumps(report.to_dict()) if args.json else format_report(report))
    return EXIT_OK if report.ok else EXIT_ERROR


def _generate(args: argparse.Namespace) -> int:
    manifest = Path(args.input)
    output = Path(args.output) if args.output else manifest.parent / "scripts"
    try:
        script = ScriptGenerator(
            manifest,
            fingerprint_db=args.fingerprint_db,
            record_fingerprints=not args.no_record,
            webhook_url=args.webhook_url,
            webhook_outbox=args.webhook_outbox,
        ).generate(args.backend, args.style, output=output)
    except (ConfigError, GenerationError, ValueError, FileNotFoundError) as exc:
        print(f"healix: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        print(f"healix: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _outbox_note(args.webhook_outbox)
    if args.json:
        print(
            json.dumps(
                {
                    "backend": script.backend,
                    "style": script.style,
                    "file_path": str(script.file_path),
                    "pages": script.page_count,
                    "element_count": script.element_count,
                    "skipped": [{"url": u, "reason": r} for u, r in script.skipped],
                    "warnings": list(script.warnings),
                }
            )
        )
        return EXIT_OK
    print(
        f"wrote {script.file_path}: {script.page_count} page(s), "
        f"{script.element_count} element(s) ({script.backend}, {script.style})"
    )
    for url, reason in script.skipped:
        print(f"  skipped {url}: {reason}")
    if script.truncated:
        print(f"  {script.truncated} element(s) beyond the per-page limit were left out")
    for warning in script.warnings:
        print(f"  warning: {warning}")
    return EXIT_OK


def _outbox_note(path: str | None) -> None:
    """Say, on stderr, when a run left webhook events in its outbox."""
    if not path or not Path(path).exists():
        return
    try:
        outbox = Outbox(path)
        try:
            counts = outbox.counts()
        finally:
            outbox.close()
    except OutboxError:
        return
    if counts[PENDING]:
        print(
            f"healix: {counts[PENDING]} webhook event(s) could not be delivered and are waiting "
            f"in {path}; send them with: healix flush-events --outbox {path} --webhook-url URL",
            file=sys.stderr,
        )
    if counts[DEAD]:
        print(
            f"healix: the webhook receiver rejected {counts[DEAD]} event(s); they are kept in "
            f"{path} (--retry-rejected on flush-events sends them again)",
            file=sys.stderr,
        )


def _flush_events(args: argparse.Namespace) -> int:
    if not Path(args.outbox).exists():
        print(f"healix: error: no outbox at {args.outbox}", file=sys.stderr)
        return EXIT_USAGE
    try:
        outbox = Outbox(args.outbox)
        try:
            revived = outbox.retry_dead() if args.retry_rejected else 0
            with DurableWebhookSender(
                args.webhook_url,
                outbox,
                secret=os.environ.get(WEBHOOK_SECRET_ENV) or None,
                retry_interval=1.0,
                max_retry_interval=5.0,
            ) as sender:
                sender.wait_until_delivered(args.timeout)
            counts = outbox.counts()
            delivered, rejected = sender.delivered, sender.failed
        finally:
            outbox.close()
    except (OutboxError, ValueError) as exc:
        print(f"healix: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    result = {
        "delivered": delivered,
        "pending": counts[PENDING],
        "rejected": rejected,
        "kept_as_rejected": counts[DEAD],
        "retried_rejected": revived,
    }
    if args.json:
        print(json.dumps(result))
    else:
        print(f"delivered {delivered} event(s); {counts[PENDING]} still waiting")
        if rejected:
            print(f"  the receiver rejected {rejected} event(s); they are kept in {args.outbox}")
    if counts[PENDING]:
        print(
            "healix: the receiver did not accept the remaining events in time",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_PARTIAL if rejected else EXIT_OK


def _report(run: Run, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(run.summary()))
        return
    print(
        f"run {run.run_id}: {run.pages_extracted} of {run.pages_discovered} pages extracted "
        f"(discovery {run.discovery_status})"
    )
    if run.blocked_on_auth:
        print(
            f"  blocked on authentication: set {USERNAME_ENV} and {PASSWORD_ENV} (in the "
            f"environment or a .env file), then re-run with --run-id {run.run_id}"
        )
    elif run.pages_failed:
        print(
            f"  {run.pages_failed} page(s) failed; re-run with --run-id {run.run_id} to retry them"
        )
    print(f"manifest: {run.manifest_path}")


if __name__ == "__main__":
    sys.exit(main())
