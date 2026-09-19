"""One crawl, triggered from the SDK and from the CLI, in real Chromium.

The point: both must produce the same results and emit the *same events*, observable
through ``on_event`` and through ``--webhook-url``.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from healix import Crawler, Extractor, cli  # noqa: E402
from healix.discovery.manifest import Manifest  # noqa: E402
from healix.events import EVENT_DATA_FIELDS  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _browser_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:
        pytest.skip(f"cannot launch Chromium: {exc}")


def write_config(path: Path, site_url: str, output: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "base_url": f"{site_url}/index.html",
                "crawl": {
                    "discovery": {"max_pages": 50},
                    "extraction": {"output_path": str(output)},
                },
            }
        )
    )
    return path


def normalized(payloads):
    """Drop what legitimately differs between two runs (timestamps, output locations)."""
    out = []
    for p in payloads:
        data = dict(p["data"])
        if "manifest_path" in data:
            data["manifest_path"] = Path(data["manifest_path"]).name
        out.append((p["event"], p["run_id"], data))
    return out


def test_the_same_crawl_from_the_sdk_and_the_cli_emits_identical_events(
    crawl_site_url, tmp_path, webhook_receiver, capsys
):
    from tests.conftest import WebhookReceiver

    sdk_hook, cli_hook = webhook_receiver, WebhookReceiver()
    try:
        # --- SDK
        sdk_events = []
        sdk_cfg = write_config(tmp_path / "sdk.json", crawl_site_url, tmp_path / "sdk_out")
        sdk_run = Crawler(
            sdk_cfg, run_id="same", on_event=sdk_events.append, webhook_url=sdk_hook.url
        ).discover_and_extract()

        # --- CLI
        cli_cfg = write_config(tmp_path / "cli.json", crawl_site_url, tmp_path / "cli_out")
        code = cli.main(
            ["crawl", "--config", str(cli_cfg), "--run-id", "same",
             "--webhook-url", cli_hook.url, "--json"]
        )  # fmt: skip
        summary = json.loads(capsys.readouterr().out)
        cli_events = cli_hook.payloads
    finally:
        cli_hook.close()

    assert code == 0

    # what the SDK's callback saw is exactly what its webhook received, timestamps included
    assert sdk_events == sdk_hook.payloads
    # and the CLI's webhook stream is the SDK's stream
    assert normalized(cli_events) == normalized(sdk_events)

    # the stream is well formed: every discovery, then every extraction, then completion
    kinds = [e["event"] for e in sdk_events]
    n = sdk_run.pages_discovered
    assert n > 5
    assert kinds == ["page_discovered"] * n + ["page_extracted"] * n + ["run_complete"]
    for e in sdk_events:
        assert list(e["data"]) == list(EVENT_DATA_FIELDS[e["event"]])
    complete = sdk_events[-1]["data"]
    assert (complete["pages_discovered"], complete["pages_extracted"]) == (n, n)

    # and both runs produced the same pages and the same files
    cli_manifest = Manifest.load(tmp_path / "cli_out" / "manifest.json")
    assert [p.url for p in cli_manifest.pages] == [p.url for p in sdk_run.pages]
    assert [p.output_file for p in cli_manifest.pages] == [p.output_file for p in sdk_run.pages]
    assert summary["pages_extracted"] == n and summary["run_id"] == "same"
    for page in sdk_run.pages:
        assert (tmp_path / "sdk_out" / page.output_file).is_file()
        assert (tmp_path / "cli_out" / page.output_file).is_file()


def test_resuming_through_the_cli_emits_no_repeat_page_events(
    crawl_site_url, tmp_path, webhook_receiver, capsys
):
    cfg = write_config(tmp_path / "run.json", crawl_site_url, tmp_path / "out")
    assert cli.main(["crawl", "--config", str(cfg), "--run-id", "r1"]) == 0
    capsys.readouterr()

    # the same output directory again: refused unless the run is named
    assert cli.main(["crawl", "--config", str(cfg)]) == 2
    assert "already holds run 'r1'" in capsys.readouterr().err

    assert (
        cli.main(
            ["crawl", "--config", str(cfg), "--run-id", "r1", "--webhook-url", webhook_receiver.url]
        )
        == 0
    )
    assert [p["event"] for p in webhook_receiver.payloads] == ["run_complete"]


def test_discover_only_then_extract_from_the_cli(
    crawl_site_url, tmp_path, webhook_receiver, capsys
):
    cfg = write_config(tmp_path / "run.json", crawl_site_url, tmp_path / "out")
    assert cli.main(["crawl", "--config", str(cfg), "--run-id", "two-step", "--discover-only"]) == 0
    capsys.readouterr()
    manifest_path = tmp_path / "out" / "manifest.json"
    assert Manifest.load(manifest_path).pages_extracted == 0
    assert not (tmp_path / "out" / "pages").exists()

    code = cli.main(
        [
            "extract",
            "--manifest",
            str(manifest_path),
            "--webhook-url",
            webhook_receiver.url,
            "--json",
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert (
        summary["run_id"] == "two-step"
        and summary["pages_extracted"] == summary["pages_discovered"]
    )
    kinds = [p["event"] for p in webhook_receiver.payloads]
    assert kinds == ["page_extracted"] * summary["pages_extracted"] + ["run_complete"]
    assert {p["run_id"] for p in webhook_receiver.payloads} == {"two-step"}


def test_extractor_sdk_class_over_a_discovered_manifest(crawl_site_url, tmp_path):
    cfg = write_config(tmp_path / "run.json", crawl_site_url, tmp_path / "out")
    Crawler(cfg, run_id="r2").discover()
    run = Extractor().extract(tmp_path / "out" / "manifest.json")
    assert run.pages_extracted == run.pages_discovered > 5
