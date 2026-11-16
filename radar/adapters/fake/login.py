"""登入的測試替身（AD-2）：開發環境的假登入「選一位成員」。

真的登入（Epic 7 的 Google、試用環境的 Access JWT）都是「確認這個請求是誰」的函式，收
`FakeLoginRequest`、回 `LoginIdentity`；這裡是同一種呼叫方式的替身，不驗任何憑證，所以只有
開發環境用（`radar/entrypoints/bootstrap.py` 的 `build_fake_login_confirm` 依設定挑）。
選的人在不在允許名單，由 `services/auth.log_in_with_fake_login` 查資料庫確認。
"""

from radar.domain.auth import ChosenMember, FakeLoginRequest, LoginRejectedError, LoginRejection


def pick_member_identity(request: FakeLoginRequest) -> ChosenMember:
    """開發環境：請求選了哪一位成員。

    Raises:
        LoginRejectedError: 沒有選人（`NO_MEMBER_CHOSEN`）。
    """
    if request.member_id is None:
        raise LoginRejectedError(LoginRejection.NO_MEMBER_CHOSEN)
    return ChosenMember(member_id=request.member_id)
