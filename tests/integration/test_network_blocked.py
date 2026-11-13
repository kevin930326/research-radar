"""確認 integration 測試只能連本機（tests/conftest.py 加的 allow_hosts）。"""

import socket

import pytest
from pytest_socket import SocketConnectBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
@pytest.mark.xfail(
    raises=SocketConnectBlockedError,
    strict=True,
    reason="預期被 pytest-socket 擋下；通過代表整合測試的網路封鎖失效（只該放行本機）",
)
def test_integration_test_cannot_connect_outside_localhost() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(("1.1.1.1", 443))
