"""R0.2: the bounded per-process limiter used while Redis is unavailable."""

from app.core.rate_limit import InMemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_counts_within_a_window() -> None:
    limiter = InMemoryRateLimiter(max_keys=10, clock=FakeClock())
    assert [limiter.hit("a", 60) for _ in range(3)] == [1, 2, 3]


def test_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter(max_keys=10, clock=FakeClock())
    limiter.hit("a", 60)
    assert limiter.hit("b", 60) == 1


def test_window_expiry_resets_the_count() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(max_keys=10, clock=clock)
    limiter.hit("a", 60)
    limiter.hit("a", 60)
    clock.now += 60
    assert limiter.hit("a", 60) == 1


def test_release_gives_an_attempt_back_and_never_goes_negative() -> None:
    limiter = InMemoryRateLimiter(max_keys=10, clock=FakeClock())
    limiter.hit("a", 60)
    limiter.hit("a", 60)

    limiter.release("a")
    assert limiter.hit("a", 60) == 2

    limiter.release("unknown")
    assert limiter.size == 1


def test_memory_is_bounded_under_a_key_flood() -> None:
    limiter = InMemoryRateLimiter(max_keys=100, clock=FakeClock())
    for index in range(10_000):
        limiter.hit(f"attacker-{index}", 60)
    assert limiter.size <= 100


def test_expired_entries_are_evicted_before_live_ones() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(max_keys=3, clock=clock)
    limiter.hit("short", 1)
    limiter.hit("victim", 60)
    limiter.hit("victim", 60)
    limiter.hit("other", 60)
    clock.now += 2

    limiter.hit("new", 60)

    assert limiter.hit("victim", 60) == 3


def test_hit_in_window_reports_the_window_it_counted_in() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(max_keys=10, clock=clock)
    assert limiter.hit_in_window("a", 60) == (1, 1060.0)
    clock.now += 10
    assert limiter.hit_in_window("a", 60) == (2, 1060.0)


def test_a_refund_for_an_ended_window_never_touches_the_next_one() -> None:
    """The login refund names the window it was counted in. Without that, a refund
    landing after its window rolled over would erase a hit from the new window."""
    clock = FakeClock()
    limiter = InMemoryRateLimiter(max_keys=10, clock=clock)
    _, old_window = limiter.hit_in_window("a", 60)
    clock.now += 60
    limiter.hit("a", 60)  # a new window starts with someone else's hit

    limiter.release("a", window=old_window)

    assert limiter.hit("a", 60) == 2


def test_a_refund_in_its_own_window_is_taken_back() -> None:
    limiter = InMemoryRateLimiter(max_keys=10, clock=FakeClock())
    limiter.hit("a", 60)
    _, window = limiter.hit_in_window("a", 60)

    limiter.release("a", window=window)

    assert limiter.hit("a", 60) == 2
