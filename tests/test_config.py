import json

import pytest

from healix import ConfigError, RunConfig
from healix.discovery.crawler import DiscoveryConfig
from healix.extraction import ExtractionConfig

FULL = {
    "mode": "guided",
    "base_url": "https://example.com",
    "start_url": "https://example.com/login",
    "crawl": {
        "discovery": {
            "domain_scope": "same_domain",
            "max_pages": 50,
            "dedupe_by": "url_normalized_and_structural_hash",
        },
        "extraction": {
            "sequence": "one_by_one",
            "output_format": "json",
            "output_path": "./output/",
            "iframe_traversal": True,
            "platform_detection": "auto",
        },
    },
    "backend": "playwright",
}


def test_the_documented_example_config_parses():
    cfg = RunConfig.from_dict(FULL)
    assert (cfg.mode, cfg.backend) == ("guided", "playwright")
    assert cfg.discovery == DiscoveryConfig(max_pages=50)
    assert cfg.extraction == ExtractionConfig(output_path="./output/")
    assert cfg.start_urls == ["https://example.com/login", "https://example.com"]


def test_minimal_autonomous_config_gets_defaults():
    cfg = RunConfig.from_dict({"base_url": "https://example.com"})
    assert cfg.mode == "autonomous"
    assert cfg.backend == "playwright"
    assert cfg.discovery == DiscoveryConfig() and cfg.extraction == ExtractionConfig()
    assert cfg.start_urls == ["https://example.com"]


def test_mode_is_inferred_from_start_url():
    assert RunConfig.from_dict({"start_url": "https://e.com/login"}).mode == "guided"
    assert RunConfig.from_dict({"base_url": "https://e.com"}).mode == "autonomous"


def test_guided_start_urls_are_deduplicated():
    cfg = RunConfig.from_dict({"start_url": "https://e.com/", "base_url": "https://e.com/"})
    assert cfg.start_urls == ["https://e.com/"]


@pytest.mark.parametrize(
    "raw, message",
    [
        ({}, "autonomous mode needs a base_url"),
        ({"mode": "guided", "base_url": "https://e.com"}, "guided mode needs a start_url"),
        ({"mode": "wander", "base_url": "https://e.com"}, "mode must be one of"),
        ({"base_url": "example.com"}, "absolute http"),
        ({"base_url": "ftp://example.com"}, "absolute http"),
        ({"base_url": "https://e.com", "backend": "cypress"}, "backend must be one of"),
        ({"base_url": "https://e.com", "colour": "blue"}, "unknown config keys"),
        ({"base_url": "https://e.com", "crawl": {"spider": {}}}, "unknown crawl keys"),
        ({"base_url": "https://e.com", "crawl": []}, "crawl must be an object"),
        (
            {"base_url": "https://e.com", "crawl": {"discovery": {"max_pages": 0}}},
            "crawl.discovery",
        ),
        ({"base_url": "https://e.com", "crawl": {"discovery": {"depth": 3}}}, "crawl.discovery"),
        (
            {"base_url": "https://e.com", "crawl": {"extraction": {"sequence": "parallel"}}},
            "crawl.extraction",
        ),
        ({"base_url": "https://e.com", "crawl": {"extraction": "json"}}, "must be an object"),
    ],
)
def test_invalid_configs_are_rejected_with_a_specific_message(raw, message):
    with pytest.raises(ConfigError, match=message):
        RunConfig.from_dict(raw)


def test_selenium_is_a_valid_backend_value():
    assert (
        RunConfig.from_dict({"base_url": "https://e.com", "backend": "selenium"}).backend
        == "selenium"
    )


@pytest.mark.parametrize(
    "key",
    ["password", "Password", "login_password", "passwd", "secret", "client_secret", "token",
     "api_key", "apiKey", "api-key", "credentials", "username"],
)  # fmt: skip
def test_credential_looking_keys_are_rejected_anywhere_in_the_config(key):
    top = {"base_url": "https://e.com", key: "x"}
    nested = {"base_url": "https://e.com", "crawl": {"discovery": {key: "x"}}}
    for raw in (top, nested):
        with pytest.raises(ConfigError, match=r"\.env"):
            RunConfig.from_dict(raw)


def test_a_credential_key_is_rejected_before_anything_else_is_validated():
    with pytest.raises(ConfigError, match="looks like a credential"):
        RunConfig.from_dict({"password": "hunter2"})


def test_the_rejection_message_never_echoes_the_secret_value():
    with pytest.raises(ConfigError) as info:
        RunConfig.from_dict({"base_url": "https://e.com", "api_key": "sk-live-VERY-SECRET"})
    assert "VERY-SECRET" not in str(info.value)


def test_extraction_only_config_does_not_need_a_start_point():
    cfg = RunConfig.from_dict(
        {"crawl": {"extraction": {"output_path": "out"}}}, require_start=False
    )
    assert cfg.extraction.output_path == "out"


def test_load_reads_a_json_file(tmp_path):
    path = tmp_path / "run_config.json"
    path.write_text(json.dumps(FULL))
    assert RunConfig.load(path) == RunConfig.from_dict(FULL)


def test_load_reports_bad_json_and_missing_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        RunConfig.load(bad)
    with pytest.raises(FileNotFoundError):
        RunConfig.load(tmp_path / "missing.json")


def test_a_non_object_document_is_rejected(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]")
    with pytest.raises(ConfigError, match="JSON object"):
        RunConfig.load(path)


def test_coerce_accepts_config_dict_path_and_none(tmp_path):
    cfg = RunConfig.from_dict(FULL)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(FULL))
    assert RunConfig.coerce(cfg) is cfg
    assert RunConfig.coerce(FULL) == cfg
    assert RunConfig.coerce(path) == cfg
    assert RunConfig.coerce(str(path)) == cfg
    assert RunConfig.coerce(None, require_start=False).extraction == ExtractionConfig()


def test_click_discovery_settings_are_read_from_the_run_config():
    config = RunConfig.from_dict(
        {
            "base_url": "https://e.com/",
            "crawl": {
                "discovery": {
                    "click_discovery": True,
                    "max_clicks_per_page": 5,
                    "max_clicks": 30,
                    "click_deny": ["escalate"],
                }
            },
        }
    )
    discovery = config.discovery
    assert discovery.click_discovery is True
    assert (discovery.max_clicks_per_page, discovery.max_clicks) == (5, 30)
    assert discovery.click_deny == ["escalate"]


def test_a_bad_click_setting_is_a_config_error_not_a_crash():
    with pytest.raises(ConfigError, match="max_clicks"):
        RunConfig.from_dict(
            {"base_url": "https://e.com/", "crawl": {"discovery": {"max_clicks": 0}}}
        )
