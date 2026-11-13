"""確認 unit 測試連不到網路（pytest-socket 的 --disable-socket）。"""

import socket

import pytest
from pytest_socket import SocketBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.xfail(
    raises=SocketBlockedError,
    strict=True,
    reason="預期被 pytest-socket 擋下；通過代表 unit 測試的網路封鎖失效",
)
def test_unit_test_cannot_open_network_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(("1.1.1.1", 443))
