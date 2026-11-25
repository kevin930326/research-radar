import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { bibtexKey, formatApa, formatBibtex, formatRis, type CitationSource } from "./citation";
import { RAIL_LABEL_CLASS } from "./parts";

// 引用（Story 3.10；EXPERIENCE.md 畫面 8 輔助欄「引用」、決定第 99 項；設計稿 paper.html「引用／已複製」、
// paper-demo.html「已複製」）。成員版與示範站共用（AR-24），格式在 citation.ts，全部在瀏覽器裡產生、不呼叫後端。
// - 「複製 APA 引用」：把 APA 引用加連結放進剪貼簿；成功後字原地換成「已複製」（暖白色、不能按）2 秒再換回來，
//   不用浮動通知（UX-DR25）。剪貼簿不能用（沒有權限、不是 https）時 EXPERIENCE.md 沒寫怎麼辦：
//   先什麼都不顯示、字不變（列給 Kevin）。
// - 「下載 RIS」「下載 BibTeX」：在瀏覽器裡產生檔案下載，按了字不變。
// 設計稿畫成連結（<a href="#">），這裡是按鈕：三個都是動作、不是到別的頁面（列給 Kevin）。
// 設計稿每個高 36px；可以按的範圍補到 44×44（ADR-0037），用上下各 -4px 的外距讓間距和設計稿一樣。
// 字放在裡面一個 36px 高的 span：三顆按鈕的按鈕框上下互相疊 8px，字的框不疊（200% 放大的重疊檢查看的是字）。

/** 「已複製」維持多久（EXPERIENCE.md 畫面 8、Component Patterns「即時回饋」）。 */
export const COPIED_MS = 2000;

const ACTION_CLASS =
  "group -my-1 inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center self-start bg-transparent p-[0px] text-left text-accent-gold aria-disabled:cursor-default aria-disabled:text-text-primary";

/** 按鈕上的字：設計稿的 36px 高；滑過時加底線（「已複製」不加）。 */
function ActionText({ children, underline = true }: { children: string; underline?: boolean }) {
  return (
    <span className={`flex min-h-[36px] items-center decoration-1 underline-offset-4 ${underline ? "group-hover:underline" : ""}`}>
      {children}
    </span>
  );
}

function downloadFile(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  // 下載開始之後才收回網址。
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function CiteBlock({ source, mobile = false }: { source: CitationSource; mobile?: boolean }) {
  const { t } = useTranslation("paper");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return undefined;
    const timer = window.setTimeout(() => setCopied(false), COPIED_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    if (copied) return;
    try {
      await navigator.clipboard.writeText(formatApa(source));
      setCopied(true);
    } catch {
      // 剪貼簿不能用：先不顯示任何東西（EXPERIENCE.md 沒寫，列給 Kevin）。
    }
  };

  const headingId = mobile ? "paper-cite-mobile" : "paper-cite";
  return (
    <section aria-labelledby={headingId} data-cite={mobile ? "mobile" : "rail"} className="mt-[28px] border-t border-hairline pt-[28px]">
      <h2 id={headingId} className={RAIL_LABEL_CLASS}>
        {t("paper.cite.heading")}
      </h2>
      <div className="mt-[14px] flex flex-col text-s14 tracking-004">
        <button
          type="button"
          data-cite="apa"
          aria-live="polite"
          aria-disabled={copied ? "true" : undefined}
          onClick={() => void copy()}
          className={ACTION_CLASS}
        >
          <ActionText underline={!copied}>{copied ? t("paper.cite.copied") : t("paper.cite.copyApa")}</ActionText>
        </button>
        <button
          type="button"
          data-cite="ris"
          onClick={() => downloadFile(`${bibtexKey(source)}.ris`, formatRis(source), "application/x-research-info-systems;charset=utf-8")}
          className={ACTION_CLASS}
        >
          <ActionText>{t("paper.cite.downloadRis")}</ActionText>
        </button>
        <button
          type="button"
          data-cite="bibtex"
          onClick={() => downloadFile(`${bibtexKey(source)}.bib`, formatBibtex(source), "application/x-bibtex;charset=utf-8")}
          className={ACTION_CLASS}
        >
          <ActionText>{t("paper.cite.downloadBibtex")}</ActionText>
        </button>
      </div>
    </section>
  );
}
