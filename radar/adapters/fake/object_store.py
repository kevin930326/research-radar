"""`ObjectStore` 的替身：放在記憶體的 dict，記下每次 put。"""

from dataclasses import dataclass, field
from io import Reader

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

    def put_stream(self, key: str, stream: Reader[bytes], *, content_type: str) -> None:
        """整個讀完才存；讀到一半丟出例外時什麼都不存（和真的物件儲存放棄分段上傳一樣）。"""
        chunks: list[bytes] = []
        while chunk := stream.read(64 * 1024):
            chunks.append(chunk)
        self.put(key, b"".join(chunks), content_type=content_type)

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFoundError(key) from None
