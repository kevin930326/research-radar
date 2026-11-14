"""測試替身（AD-2）：和真實的 adapter 同一種呼叫方式，不連資料庫、不連網路。"""

from radar.adapters.fake.heartbeat import FakeHeartbeat
from radar.adapters.fake.jobs import FakeJobQueue
from radar.adapters.fake.object_store import FakeObjectStore, FakePut
from radar.adapters.fake.paper_source import FakePaperSource, FakeRequest

__all__ = [
    "FakeHeartbeat",
    "FakeJobQueue",
    "FakeObjectStore",
    "FakePaperSource",
    "FakePut",
    "FakeRequest",
]
