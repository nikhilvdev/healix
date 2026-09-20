"""``healix flush-events`` and the outbox hint after a run."""

import json

import pytest

from healix import cli
from healix.events import Outbox
from tests.conftest import WebhookReceiver

DEAD_URL = "http://127.0.0.1:1/hook"


def fill(path, count=3):
    outbox = Outbox(path)
    for n in range(count):
        outbox.add("page_discovered", "r", json.dumps({"n": n}).encode())
    outbox.close()


@pytest.fixture(autouse=True)
def no_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("HEALIX_WEBHOOK_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)


def test_flush_sends_everything_waiting_in_order_and_empties_the_outbox(
    tmp_path, webhook_receiver, capsys
):
    fill(tmp_path / "events.db")
    code = cli.main(
        [
            "flush-events",
            "--outbox",
            str(tmp_path / "events.db"),
            "--webhook-url",
            webhook_receiver.url,
        ]
    )
    assert code == 0
    assert [p["n"] for p in webhook_receiver.payloads] == [0, 1, 2]
    assert "delivered 3 event(s); 0 still waiting" in capsys.readouterr().out
    assert Outbox(tmp_path / "events.db").counts() == {"pending": 0, "dead": 0}


def test_flush_signs_with_the_secret_from_the_environment(tmp_path, webhook_receiver, monkeypatch):
    monkeypatch.setenv("HEALIX_WEBHOOK_SECRET", "s3cret")
    fill(tmp_path / "events.db", 1)
    cli.main(
        [
            "flush-events",
            "--outbox",
            str(tmp_path / "events.db"),
            "--webhook-url",
            webhook_receiver.url,
        ]
    )
    assert webhook_receiver.requests[0]["headers"]["X-Healix-Signature"].startswith("sha256=")


def test_flush_json_reports_the_counts(tmp_path, webhook_receiver, capsys):
    fill(tmp_path / "events.db", 2)
    cli.main(
        [
            "flush-events", "--outbox", str(tmp_path / "events.db"),
            "--webhook-url", webhook_receiver.url, "--json",
        ]
    )  # fmt: skip
    assert json.loads(capsys.readouterr().out) == {
        "delivered": 2,
        "pending": 0,
        "rejected": 0,
        "kept_as_rejected": 0,
        "retried_rejected": 0,
    }


def test_flush_exits_1_and_keeps_the_events_when_the_receiver_is_still_down(tmp_path, capsys):
    fill(tmp_path / "events.db", 2)
    code = cli.main(
        [
            "flush-events", "--outbox", str(tmp_path / "events.db"),
            "--webhook-url", DEAD_URL, "--timeout", "1",
        ]
    )  # fmt: skip
    assert code == 1
    assert Outbox(tmp_path / "events.db").counts()["pending"] == 2
    assert "2 still waiting" in capsys.readouterr().out


def test_flush_exits_3_when_the_receiver_rejects_an_event(tmp_path, webhook_receiver, capsys):
    webhook_receiver.respond_with(200, 400, 200)
    fill(tmp_path / "events.db")
    code = cli.main(
        [
            "flush-events",
            "--outbox",
            str(tmp_path / "events.db"),
            "--webhook-url",
            webhook_receiver.url,
        ]
    )
    assert code == 3
    assert Outbox(tmp_path / "events.db").counts() == {"pending": 0, "dead": 1}
    assert "rejected 1 event(s)" in capsys.readouterr().out


def test_flush_can_send_the_rejected_ones_again(tmp_path, webhook_receiver, capsys):
    webhook_receiver.respond_with(400)
    fill(tmp_path / "events.db", 1)
    args = [
        "flush-events",
        "--outbox",
        str(tmp_path / "events.db"),
        "--webhook-url",
        webhook_receiver.url,
    ]
    assert cli.main(args) == 3
    assert cli.main([*args, "--retry-rejected", "--json"]) == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["retried_rejected"] == 1
    assert len(webhook_receiver.requests) == 2
    assert Outbox(tmp_path / "events.db").counts() == {"pending": 0, "dead": 0}


def test_flush_says_so_when_there_is_no_outbox(tmp_path, capsys):
    code = cli.main(
        ["flush-events", "--outbox", str(tmp_path / "nope.db"), "--webhook-url", DEAD_URL]
    )
    assert code == 2
    assert "no outbox" in capsys.readouterr().err


def test_flush_says_so_when_the_file_is_not_an_outbox(tmp_path, capsys):
    (tmp_path / "junk.db").write_text("not a database")
    code = cli.main(
        ["flush-events", "--outbox", str(tmp_path / "junk.db"), "--webhook-url", DEAD_URL]
    )
    assert code == 2
    assert "junk.db" in capsys.readouterr().err


def test_flush_needs_an_outbox_and_a_url(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["flush-events", "--webhook-url", DEAD_URL])
    assert info.value.code == 2


def test_a_run_says_what_it_left_in_the_outbox(tmp_path, capsys):
    fill(tmp_path / "events.db", 2)
    cli._outbox_note(str(tmp_path / "events.db"))
    err = capsys.readouterr().err
    assert "2 webhook event(s) could not be delivered" in err
    assert "healix flush-events --outbox" in err


def test_a_run_says_nothing_when_the_outbox_is_empty_or_absent(tmp_path, capsys):
    Outbox(tmp_path / "empty.db").close()
    cli._outbox_note(str(tmp_path / "empty.db"))
    cli._outbox_note(str(tmp_path / "absent.db"))
    cli._outbox_note(None)
    assert capsys.readouterr().err == ""


def test_the_receiver_can_be_down_at_first_and_flush_works_once_it_is_back(tmp_path):
    fill(tmp_path / "events.db", 2)
    down = ["flush-events", "--outbox", str(tmp_path / "events.db"), "--webhook-url", DEAD_URL]
    assert cli.main([*down, "--timeout", "1"]) == 1
    receiver = WebhookReceiver()
    try:
        up = [
            "flush-events",
            "--outbox",
            str(tmp_path / "events.db"),
            "--webhook-url",
            receiver.url,
        ]
        assert cli.main(up) == 0
        assert [p["n"] for p in receiver.payloads] == [0, 1]
    finally:
        receiver.close()


def test_an_outbox_without_a_webhook_url_is_a_usage_error(tmp_path, capsys):
    config = tmp_path / "run.json"
    config.write_text(json.dumps({"base_url": "https://e.com/"}))
    code = cli.main(["crawl", "--config", str(config), "--webhook-outbox", str(tmp_path / "e.db")])
    assert code == 2
    assert "webhook_url" in capsys.readouterr().err
