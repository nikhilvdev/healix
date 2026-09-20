"""``healix generate`` and ``healix doctor`` on the command line."""

import ast
import json

import pytest

from healix import cli
from healix.discovery.manifest import Manifest
from tests.generation.pages import write_run


@pytest.fixture
def manifest(tmp_path):
    return write_run(tmp_path / "output")


def run_cli(*argv):
    return cli.main([str(a) for a in argv])


# --- generate ------------------------------------------------------------------------------ #


def test_generate_writes_a_script_beside_the_manifest_by_default(manifest, capsys, monkeypatch):
    monkeypatch.chdir(manifest.parent)  # the fingerprint database lands in the working directory
    assert run_cli("generate", "--input", manifest) == 0
    script = manifest.parent / "scripts" / "pages_playwright.py"
    ast.parse(script.read_text())
    out = capsys.readouterr().out
    assert str(script) in out and "3 page(s)" in out and "11 element(s)" in out
    assert (manifest.parent / "healix.db").exists()  # fingerprints recorded for healing


@pytest.mark.parametrize(
    ("backend", "style", "filename"),
    [
        ("playwright", "pom", "pages_playwright.py"),
        ("selenium", "test", "test_healix_selenium.py"),
        ("selenium", "action", "actions_selenium.py"),
    ],
)
def test_backend_style_and_output_are_options(manifest, tmp_path, backend, style, filename):
    out = tmp_path / "elsewhere"
    code = run_cli(
        "generate", "--input", manifest, "--backend", backend, "--style", style,
        "--output", out, "--fingerprint-db", tmp_path / "fp.db",
    )  # fmt: skip
    assert code == 0
    assert backend.capitalize() in (out / filename).read_text()


def test_generate_prints_json_and_lists_skipped_pages(manifest, tmp_path, capsys):
    loaded = Manifest.load(manifest)
    loaded.pages[1].status = "failed"
    loaded.save(manifest)
    assert run_cli("generate", "--input", manifest, "--json", "--no-record") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend"] == "playwright" and result["style"] == "pom"
    assert result["pages"] == 2
    assert result["skipped"][0]["reason"] == "status is failed"


def test_generate_warns_when_the_script_has_no_fingerprints_to_heal_with(
    manifest, tmp_path, capsys
):
    empty = tmp_path / "x.db"
    assert run_cli("generate", "--input", manifest, "--no-record", "--fingerprint-db", empty) == 0
    out = capsys.readouterr().out
    assert "warning:" in out and "no fingerprints" in out

    run_cli("generate", "--input", manifest, "--no-record", "--fingerprint-db", empty, "--json")
    assert "no fingerprints" in json.loads(capsys.readouterr().out)["warnings"][0]

    run_cli("generate", "--input", manifest, "--fingerprint-db", empty)  # now record them
    capsys.readouterr()
    run_cli("generate", "--input", manifest, "--no-record", "--fingerprint-db", empty, "--json")
    assert json.loads(capsys.readouterr().out)["warnings"] == []


def test_no_record_leaves_the_fingerprint_store_alone(manifest, tmp_path):
    db = tmp_path / "fp.db"
    assert run_cli("generate", "--input", manifest, "--fingerprint-db", db, "--no-record") == 0
    assert not db.exists()


def test_generate_emits_the_event_to_a_webhook(manifest, webhook_receiver):
    code = run_cli(
        "generate", "--input", manifest, "--no-record", "--webhook-url", webhook_receiver.url
    )
    assert code == 0
    [payload] = webhook_receiver.payloads
    assert payload["event"] == "script_generated" and payload["run_id"] == "run-1"
    assert payload["data"]["backend"] == "playwright" and payload["data"]["element_count"] == 11


@pytest.mark.parametrize(
    "argv",
    [
        ["generate"],
        ["generate", "--input", "x.json", "--backend", "cypress"],
        ["generate", "--input", "x.json", "--style", "bdd"],
    ],
)
def test_generate_argument_errors_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        run_cli(*argv)
    assert raised.value.code == 2


def test_a_missing_manifest_is_a_usage_error_with_a_message(tmp_path, capsys):
    assert run_cli("generate", "--input", tmp_path / "nope.json") == 2
    assert "no manifest" in capsys.readouterr().err


def test_a_manifest_with_nothing_extracted_says_what_to_do(tmp_path, capsys):
    pending = write_run(tmp_path, status="pending")
    assert run_cli("generate", "--input", pending, "--no-record") == 2
    assert "run the extraction first" in capsys.readouterr().err


# --- doctor -------------------------------------------------------------------------------- #


def test_doctor_reports_and_exits_zero_when_a_backend_works(capsys, monkeypatch):
    from healix import doctor
    from healix.driver.diagnose import BackendReport

    def diagnose(backend):
        return BackendReport(backend, True, "1.0", browser=f"/opt/{backend}")

    monkeypatch.setattr(doctor, "diagnose_backend", diagnose)
    assert run_cli("doctor") == 0
    out = capsys.readouterr().out
    assert "Ready. Usable backends: playwright, selenium." in out


def test_doctor_json(capsys, monkeypatch):
    from healix import doctor
    from healix.driver.diagnose import BackendReport

    monkeypatch.setattr(
        doctor,
        "diagnose_backend",
        lambda b: (
            BackendReport(b, True, "1.0", browser="/x")
            if b == "selenium"
            else BackendReport(b, False, problem="missing", hint="pip install x")
        ),
    )
    assert run_cli("doctor", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["ready_backends"] == ["selenium"]
    playwright = next(c for c in report["checks"] if c["name"] == "playwright")
    assert playwright["status"] == "missing" and playwright["hint"] == "pip install x"
