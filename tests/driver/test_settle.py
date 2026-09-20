"""The quiet-window wait, without a browser: a fake clock and a scripted page."""

from healix.driver.settle import POLL_SECONDS, wait_until_quiet


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def watch(readings, *, quiet_s, timeout_s, initial=None):
    """Run the wait against ``readings`` (a function of time) and say when it stopped."""
    clock = Clock()
    result = wait_until_quiet(
        lambda: readings(clock.now),
        quiet_s=quiet_s,
        deadline=timeout_s,
        initial=initial,
        clock=clock,
        sleep=clock.sleep,
    )
    return result, clock.now


def test_a_page_that_never_changes_is_quiet_after_the_quiet_window():
    result, at = watch(lambda t: "a", quiet_s=0.5, timeout_s=10)
    assert result is True
    assert 0.5 <= at < 0.5 + 2 * POLL_SECONDS


def test_a_change_restarts_the_quiet_window():
    # It changes at 0.3 s, before the first quiet window is up, so that window does not count: the
    # page cannot be called quiet before 0.3 + 0.5.
    result, at = watch(lambda t: "a" if t < 0.3 else "b", quiet_s=0.5, timeout_s=10)
    assert result is True
    assert at >= 0.8


def test_a_page_that_keeps_changing_runs_out_of_time():
    result, at = watch(lambda t: str(int(t / POLL_SECONDS)), quiet_s=0.5, timeout_s=2)
    assert result is False
    assert 2 <= at < 2 + 2 * POLL_SECONDS


def test_the_wait_never_outlasts_the_deadline_for_a_quiet_window_that_cannot_fit():
    result, at = watch(lambda t: "a", quiet_s=5, timeout_s=1)
    assert result is False
    assert at < 1 + 2 * POLL_SECONDS


def test_an_initial_reading_counts_as_the_starting_state():
    # The page changed between the caller's reading and the first poll, so it is not yet quiet.
    result, at = watch(lambda t: "b", quiet_s=0.5, timeout_s=10, initial="a")
    assert result is True
    assert at >= 0.5 + POLL_SECONDS


def test_a_deadline_already_passed_reads_once_and_returns():
    reads = []
    clock = Clock()
    clock.now = 5
    result = wait_until_quiet(
        lambda: reads.append(1) or "a",
        quiet_s=0.5,
        deadline=1,
        clock=clock,
        sleep=clock.sleep,
    )
    assert result is False
    assert len(reads) == 1
