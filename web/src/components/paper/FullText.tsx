import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { FullTextLinks } from "../../generated/model";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { RAIL_LABEL_CLASS } from "./parts";

// 取得全文（EXPERIENCE.md 畫面 8 第 6 點；設計稿 paper.html 的輔助欄與手機版）：
// - 900px 以上在輔助欄：「開啟全文」（主要按鈕，開 DOI 連結）＋「連學校 VPN 時會直接到全文頁」＋有連結時的「開放取用版本」。
// - 899px 以下：書目與分類下方一塊（VPN 那句改成「下方的…」＋開放取用版本），「開啟全文」在底部固定列。
// - 沒有 DOI（也沒有開放取用版本）：一行「這篇沒有 DOI，沒有全文連結」（設計稿沒畫，沿用輔助欄「我的 PDF」那一行小字的樣子），沒有按鈕與底部固定列。
// - 「開放取用版本」只在 API 有給連結時出現（3.6 的 API 目前一律是 null，Unpaywall 還沒接）。
// - 手機底部的「加入我的文獻庫」與輔助欄的「我的文獻庫」在 LibraryBlock.tsx（Story 3.17）；引用、我的 PDF 屬於 3.10、Epic 6（AR-67）。
// 「開啟全文」是按鈕（設計稿是 <button>），按下開新分頁；不帶來源頁與 window.opener。

function openInNewTab(url: string) {
  window.open(url, "_blank", "noopener,noreferrer");
}

/** 站外的小字連結（開放取用版本）：14px、字距 0.04em；設計稿 36px 高，至少 44×44 用上下各 -4px 的外距補。 */
function RailLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      aria-describedby={NEW_TAB_NOTE_ID}
      rel="noopener noreferrer"
      className="-my-1 inline-flex min-h-tap-target min-w-tap-target items-center text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
    >
      {children}
      <ExternalArrow />
    </a>
  );
}

function OpenAccessLink({ url }: { url: string | null }) {
  const { t } = useTranslation("paper");
  if (url === null) {
    return null;
  }
  return (
    <div data-full-text="open-access" className="mt-[6px] text-s14 tracking-004">
      <RailLink href={url}>{t("paper.fullText.openAccess")}</RailLink>
    </div>
  );
}

function NoDoiLine() {
  const { t } = useTranslation("paper");
  return (
    <div data-full-text="no-doi" className="text-s13 leading-170 text-text-secondary">
      {t("paper.fullText.noDoi")}
    </div>
  );
}

/** 輔助欄（900px 以上）的「取得全文」。 */
export function FullTextRail({ links }: { links: FullTextLinks }) {
  const { t } = useTranslation("paper");
  const doiUrl = links.doi_url;
  return (
    <section aria-labelledby="paper-full-text" data-full-text="rail">
      <h2 id="paper-full-text" className={RAIL_LABEL_CLASS}>
        {t("paper.fullText.heading")}
      </h2>
      <div className="mt-[14px]">
        {doiUrl === null ? (
          links.open_access_url === null ? <NoDoiLine /> : null
        ) : (
          <>
            <button
              type="button"
              aria-describedby={NEW_TAB_NOTE_ID}
              data-full-text="open"
              onClick={() => openInNewTab(doiUrl)}
              className="inline-flex h-button-height-compact w-full cursor-pointer items-center justify-center rounded-full bg-text-primary px-[20px] font-body text-button text-s14 tracking-006 text-on-button transition-transform duration-250 hover:-translate-y-px"
            >
              {t("paper.fullText.open")}
              <ExternalArrow />
            </button>
            <div className="mt-3 text-s13 leading-160 text-text-tertiary">{t("paper.fullText.vpn")}</div>
          </>
        )}
        <OpenAccessLink url={links.open_access_url} />
      </div>
    </section>
  );
}

/** 手機版（899px 以下）書目與分類下方的「取得全文」：按鈕在底部固定列。 */
export function FullTextMobile({ links }: { links: FullTextLinks }) {
  const { t } = useTranslation("paper");
  return (
    <section aria-labelledby="paper-full-text-mobile" data-full-text="mobile" className="mt-[36px] border-t border-hairline pt-8">
      <h2 id="paper-full-text-mobile" className={RAIL_LABEL_CLASS}>
        {t("paper.fullText.heading")}
      </h2>
      <div className="mt-[14px]">
        {links.doi_url === null ? (
          links.open_access_url === null ? <NoDoiLine /> : null
        ) : (
          <div className="text-s13 leading-160 text-text-tertiary">{t("paper.fullText.vpnBelow")}</div>
        )}
        <OpenAccessLink url={links.open_access_url} />
      </div>
    </section>
  );
}

/**
 * 手機版的底部固定列：「開啟全文」＋`extra`（成員版還沒加入時的「加入我的文獻庫」，Story 3.17；已加入時只剩「開啟全文」）。
 * 沒有 DOI 時整列不出現（「加入我的文獻庫」改在書目下面的輔助區，見 LibraryMobile）。
 * `extra` 由成員版的論文頁（pages/PaperPage.tsx）放進來：這個檔不 import 我的文獻庫的元件，示範站的建置才不會帶到它（Story 3.9）。
 */
export function FullTextBar({ links, extra }: { links: FullTextLinks; extra?: ReactNode }) {
  const { t } = useTranslation("paper");
  const doiUrl = links.doi_url;
  if (doiUrl === null) {
    return null;
  }
  return (
    <div
      data-full-text="bar"
      className="sticky bottom-[0px] -mx-gutter-mobile mt-12 flex gap-3 border-t border-hairline bg-background px-gutter-mobile pt-3 pb-[20px] tablet:-mx-gutter-tablet tablet:px-gutter-tablet split:hidden"
    >
      <button
        type="button"
        aria-describedby={NEW_TAB_NOTE_ID}
        data-full-text="open-mobile"
        onClick={() => openInNewTab(doiUrl)}
        className="inline-flex h-button-height flex-1 cursor-pointer items-center justify-center rounded-full bg-text-primary px-3 font-body text-button text-on-button transition-transform duration-250 hover:-translate-y-px"
      >
        {t("paper.fullText.open")}
        <ExternalArrow />
      </button>
      {extra}
    </div>
  );
}
