import { Button } from "@base-ui/react/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { BrandName, RadarIcon } from "../components/Brand";
import { Offline } from "../components/states/Offline";
import { FIRST_PAGE, clearMemberQueries, memberSession, safeNextPath, type LoginSearch } from "../routes/session";

// 畫面 10「登入」（State Patterns「10. 登入」、Story 3.5）：開發與試用期間是假登入頁（AR-67、ADR-0046）。
// 沒有導覽列。品牌、一句說明、主要按鈕「用 Google 帳號登入」。按下去變「登入中…」並停用；
// 沒有成功時按鈕上方一行「沒有登入成功，再試一次」；帳號不在名單裡時一行說明，按鈕還在、可以再登入一次。
// 「回示範站」在示範站首頁（Epic 13）做好才放（AR-67）。
// 本來在站內、登入過期才被送回來時，同一個位置一行「登入過期，請重新登入」（設計稿沒有這個狀態，
// 照同一頁的訊息行做）。登入後回到原本那一頁（網址的 next，只收站內路徑），沒有就到使用須知。
// 開發環境要用哪一位測試成員：網址加 ?member=<成員 ID>，沒加就用假登入清單的第一位（沒有選人的畫面，設計稿沒有）。
//
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的「登入」：
// 900px 以上：360 寬的一欄置中、內容置中；圖示 38、品牌 44px；說明在品牌下 26px；訊息行的位置在說明下 36px、
//   按鈕上 20px（沒有訊息時那一格高度是 0）。
// 899px 以下：最寬 430、左右 20、內容靠左；圖示 30、品牌 34px；說明在品牌下 20px；訊息行在說明下 28px。
// 整塊上下置中，下方多留 60px（手機 80px）。品牌不是頁面標題（不用 page-title-zh），但每頁要剛好一個第一層標題
// （ADR-0037），所以品牌包在 h1 裡，樣子不變。

type Note = "failed" | "denied" | "expired";

/** 訊息行左邊的圖示（設計稿 .ln：13px、往下 4px）。 */
function NoteIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true" className="shrink-0">
      <circle cx="6.5" cy="6.5" r="5.6" stroke="currentColor" strokeWidth="0.9" />
      <path d="M6.5 5.8v3.4" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
      <circle cx="6.5" cy="3.9" r="0.65" fill="currentColor" />
    </svg>
  );
}

/** 按鈕上方的一行訊息：沒有登入成功是錯誤（紅色圖示、alert），其他是說明（灰色圖示、status）。 */
function LoginNote({ note }: { note: Note }) {
  const { t } = useTranslation(["login", "errors"]);
  const isError = note === "failed";
  const text =
    note === "failed"
      ? t("login:login.note.failed")
      : note === "denied"
        ? t("login:login.note.denied")
        : t("errors:not_authenticated");
  return (
    <div role={isError ? "alert" : "status"} data-login-note={note} className="flex items-start gap-[10px] text-left">
      <span className={`flex pt-[4px] ${isError ? "text-danger" : "text-text-secondary"}`}>
        <NoteIcon />
      </span>
      <div className="text-s15 leading-170 tracking-0 text-text-primary">{text}</div>
    </div>
  );
}

const BUTTON_CLASS =
  "inline-flex h-button-height w-full cursor-pointer items-center justify-center rounded-full bg-text-primary px-[28px] font-body text-button text-on-button transition-transform duration-250 hover:not-data-[disabled]:-translate-y-px data-[disabled]:cursor-default data-[disabled]:opacity-55";

export function LoginPage({ search }: { search: LoginSearch }) {
  const { t } = useTranslation("login");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const login = useMutation({
    mutationFn: () => memberSession().logIn({ memberId: search.member, next: search.next }),
    onSuccess: async (outcome) => {
      if (outcome.kind === "signedIn") {
        // 可能換了一個人（已登入時用 ?member= 換測試成員）：先清掉上一位的查詢快取。
        await clearMemberQueries(queryClient);
        // 後端已經只收站內路徑；這裡再檢查一次（也不回登入頁自己）。
        await navigate({ href: safeNextPath(outcome.next) ?? FIRST_PAGE, replace: true });
        // 換頁後又被送回登入頁（例如 cookie 沒存進去）時，按鈕不能停在「登入中…」。
        login.reset();
      }
    },
  });

  const outcome = login.data?.kind;
  const pending = login.isPending || outcome === "signedIn";
  // 按下登入之後就不再顯示「登入已經過期」（設計稿：按下去先清掉訊息行）。
  const note: Note | null =
    outcome === "failed" || login.isError
      ? "failed"
      : outcome === "denied"
        ? "denied"
        : login.isIdle && search.expired === true
          ? "expired"
          : null;

  return (
    <main className="flex min-h-screen flex-col justify-center pb-[80px] leading-normal split:items-center split:px-gutter-desktop split:pb-[60px]">
      <div className="mx-auto flex w-full max-w-[430px] flex-col items-start px-gutter-mobile split:w-[360px] split:max-w-full split:items-center split:px-[0px]">
        <Offline />
        <h1 className="flex items-center gap-[14px] split:gap-[18px]">
          <RadarIcon className="size-[30px] split:size-[38px]" />
          <BrandName className="text-s34 tracking-001 split:text-s44" />
        </h1>
        <p className="mt-[20px] text-s16 leading-180 tracking-004 text-text-secondary split:mt-[26px]">
          {t("login.page.subtitle")}
        </p>
        <div className="mt-[28px] flex w-full flex-col justify-end split:mt-[36px]">
          {note === null ? null : <LoginNote note={note} />}
        </div>
        <div className="mt-[20px] w-full">
          <Button
            focusableWhenDisabled
            disabled={pending}
            aria-busy={pending || undefined}
            onClick={() => login.mutate()}
            className={BUTTON_CLASS}
          >
            {pending ? t("login.button.pending") : t("login.button.idle")}
          </Button>
        </div>
      </div>
    </main>
  );
}
