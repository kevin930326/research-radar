"""確認 integration 測試只能連本機（tests/conftest.py 加的 allow_hosts）。

目標是 192.0.2.1（TEST-NET-1，保留給文件、不會有人回應）：萬一封鎖失效，
也只會逾時（TimeoutError／OSError），不會真的連到外面的主機；strict xfail 照樣判失敗。
"""

import socket

import pytest
from pytest_socket import SocketConnectBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.xfail(
    raises=SocketConnectBlockedError,
    strict=True,
    reason="預期被 pytest-socket 擋下；沒被擋（意外通過或逾時）代表整合測試的網路封鎖失效",
)
def test_integration_test_cannot_connect_outside_localhost() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(("192.0.2.1", 443))
