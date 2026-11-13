"""確認 unit 測試連不到網路（pytest-socket 的 --disable-socket）。

目標是 192.0.2.1（TEST-NET-1，保留給文件、不會有人回應）：萬一封鎖失效，
也只會逾時（TimeoutError／OSError），不會真的連到外面的主機；strict xfail 照樣判失敗。
"""

import socket

import pytest
from pytest_socket import SocketBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.xfail(
    raises=SocketBlockedError,
    strict=True,
    reason="預期被 pytest-socket 擋下；沒被擋（意外通過或逾時）代表 unit 測試的網路封鎖失效",
)
def test_unit_test_cannot_open_network_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(("192.0.2.1", 443))
