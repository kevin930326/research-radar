"""心跳的替身：記下送了哪些檢查名稱。"""


class FakeHeartbeat:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, check: str) -> None:
        self.sent.append(check)
