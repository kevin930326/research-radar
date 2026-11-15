// 登出（State Patterns「18. 帳號選單」）：帳號選單與手機的滿版選單共用。
// 登出中「登出」變成「登出中…」並停用；沒有成功時選單裡一行「沒有登出成功，再試一次」，選單不關；
// 成功回登入頁（AR-67：開發與試用期間是假登入頁），清掉所有查詢快取（下一位登入的人看不到這一位的資料；
// 之後打開成員版的網址是「還沒登入」，不是「登入過期」）。
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";

import { clearMemberQueries, memberSession } from "../routes/session";

export function useLogOut() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: () => memberSession().logOut(),
    onSuccess: async () => {
      await clearMemberQueries(queryClient);
      await navigate({ to: "/login", replace: true });
    },
  });
}
