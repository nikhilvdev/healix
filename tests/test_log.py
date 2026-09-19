import pytest
from logquill import CollectingTransport, ConsoleTransport, Level

from healix.log import (
    DEFAULT_LEVEL,
    LEVEL_ENV_VAR,
    ROOT_NAME,
    configure_logging,
    get_logger,
    level_from_env,
)


def test_get_logger_names_and_caching():
    assert get_logger().name == ROOT_NAME
    assert get_logger("healix") is get_logger()
    crawler = get_logger("healix.discovery.crawler")
    assert crawler.name == "healix.discovery.crawler"
    assert get_logger("healix.discovery.crawler") is crawler


def test_defaults_are_warn_to_a_console_transport_on_stderr():
    root = get_logger()
    assert DEFAULT_LEVEL == Level.WARN
    assert any(isinstance(t, ConsoleTransport) for t in root.transports)


def test_configure_reaches_loggers_created_before_configuration(captured_logs):
    early = get_logger("healix.test.early")  # created before configure ran
    early.info("visible", k=1)
    assert [(r["logger"], r["message"], r["meta"]) for r in captured_logs] == [
        ("healix.test.early", "visible", {"k": 1})
    ]


def test_configure_level_filters_and_restores(captured_logs):
    logger = get_logger("healix.test.level")
    configure_logging(level="warn")
    logger.info("dropped")
    logger.warn("kept")
    assert [r["message"] for r in captured_logs] == ["kept"]


def test_configure_none_leaves_settings_alone():
    root = get_logger()
    before = (list(root.transports), root.level)
    configure_logging()
    assert (root.transports, root.level) == before


def test_empty_transports_silence_healix(capsys):
    root = get_logger()
    saved = (root.transports, root.level)
    try:
        configure_logging(level="trace", transports=[])
        get_logger("healix.test.silent").error("nobody hears this")
    finally:
        configure_logging(level=saved[1], transports=saved[0])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_child_loggers_share_the_configured_transports(captured_logs):
    transport = CollectingTransport()
    configure_logging(transports=[transport])
    get_logger("healix.test.a").info("one")
    get_logger("healix.test.b").info("two")
    assert [r["message"] for r in transport.records] == ["one", "two"]
    assert captured_logs == []


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({}, Level.WARN),
        ({LEVEL_ENV_VAR: "debug"}, Level.DEBUG),
        ({LEVEL_ENV_VAR: "ERROR"}, Level.ERROR),
    ],
)
def test_level_from_env(environ, expected):
    assert level_from_env(environ) == expected


def test_invalid_env_level_warns_and_falls_back():
    with pytest.warns(UserWarning, match="HEALIX_LOG_LEVEL"):
        assert level_from_env({LEVEL_ENV_VAR: "loud"}) == Level.WARN
