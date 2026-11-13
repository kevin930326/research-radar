"""連線池的設定（ADR-0003 決定第 4 點）；不真的連線。"""

from sqlalchemy.pool import QueuePool

from radar.repo.db import Database


def test_queue_pool_uses_budget_without_overflow() -> None:
    database = Database("postgresql://radar_app@db:5432/radar", connect_timeout=4, pool_size=8)
    pool = database._engine.pool

    assert isinstance(pool, QueuePool)
    assert pool.size() == 8
    assert pool._max_overflow == 0
    assert pool.timeout() == 4
    assert pool._pre_ping is True


def test_pool_size_is_passed_through() -> None:
    database = Database("postgresql://radar_app@db:5432/radar", connect_timeout=3, pool_size=2)
    pool = database._engine.pool

    assert isinstance(pool, QueuePool)
    assert pool.size() == 2
