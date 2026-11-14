"""`ObjectStore` 的替身：放在記憶體的 dict，記下每次 put。"""

from dataclasses import dataclass, field

from radar.domain.storage_keys import ObjectNotFoundError


@dataclass(frozen=True)
class FakePut:
    key: str
    data: bytes
    content_type: str


@dataclass
class FakeObjectStore:
    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    puts: list[FakePut] = field(default_factory=list)

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts.append(FakePut(key, data, content_type))
        self.objects[key] = data
        self.content_types[key] = content_type

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFoundError(key) from None
