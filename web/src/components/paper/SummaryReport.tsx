import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { SummaryReportBody } from "../../generated/model";
import { FailedLine, LINK_CLASS, OneLineField, PickGroup, SendActions, type PickOption } from "./member-form";
import type { ZhSummaryParts } from "./ZhSummarySection";

// 回報中文重點錯誤（Story 3.12；EXPERIENCE.md 畫面 8 第 3 點、決定第 95、97、98 項；設計稿 paper.html「回報錯誤」四個狀態）。
// 成員版才有：只有 pages/PaperPage.tsx（成員版）import 這個檔，示範站的建置不會帶到（e2e/build-output.spec.ts 檢查
// `data-summary-report`）。
// - 中文重點標題列右邊「回報錯誤」（設計稿是 <a>，照修正分類做成按鈕；看起來 28px 高，按鈕本身 44×44）。表單開著時不顯示。
// - 按了在標題列下面原地展開：「欄位」四個標籤（只能選一個，role=radio）、「說明」一行（必填，100 字以內，字數算 code point，
//   和後端一樣）、「送出回報」「取消」。選了欄位也寫了說明（去掉前後空白後有字）才能按「送出回報」。
//   「剩 N 字」的門檻與樣子沒有設計稿（README 要求先問 Kevin），先不做，只擋 100 字。
// - 送出中：按鈕寫「送出中…」、標籤與說明不能改、「取消」不顯示。沒有成功：按鈕上方一行「沒有送出成功，再試一次」，
//   選的與寫的都留著。成功：表單收起來，外層換上 API 回的中文重點區塊（那一欄內容下面多一行「有成員回報可能有誤」）。
// 鍵盤：打開表單時焦點移到第一個標籤；表單收起（取消或送出成功）時焦點回到「回報錯誤」。

/** 表單送出的內容（欄位、說明）；畫面上那一筆中文重點的 `zh_summary_id` 由外層補上。 */
export type SummaryReportDraft = Omit<SummaryReportBody, "zh_summary_id">;

/** 回報錯誤要的動作：由成員版的路由（routes/summary-report.ts）給。 */
export interface SummaryReportControl {
  /** 送出：成功回 true（外層已經換上新的中文重點區塊），沒有成功回 false。不丟例外。 */
  send: (body: SummaryReportDraft) => Promise<boolean>;
}

type FormState = { field: string | null; note: string; status: "idle" | "sending" | "failed" };

const FIELD_LABELS = [
  ["research_question", "paper.summary.researchQuestion"],
  ["method", "paper.summary.method"],
  ["theory", "paper.summary.theory"],
  ["findings", "paper.summary.findings"],
] as const;

/** 中文重點那一塊（成員版、ready）：見檔頭。 */
export function SummaryReportArea({ parts, control }: { parts: ZhSummaryParts; control: SummaryReportControl }) {
  const { t } = useTranslation("paper");
  const [form, setForm] = useState<FormState | null>(null);
  // 連點兩下時第二下還看不到 sending（同一次畫面），用 ref 擋（同 routes/member.tsx 的 usePaperLibrary）。
  const sending = useRef(false);
  // 表單打開或收起之後焦點要去哪裡（使用者按了才移；第一次畫面不動焦點）。
  const [focusTo, setFocusTo] = useState<{ target: "form" | "open"; token: number } | null>(null);
  const formRef = useRef<HTMLDivElement | null>(null);
  const openRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (focusTo === null) return;
    if (focusTo.target === "open") {
      openRef.current?.focus();
    } else {
      formRef.current?.querySelector<HTMLButtonElement>("[data-report-pick]")?.focus();
    }
  }, [focusTo]);
  const moveFocus = (target: "form" | "open") => setFocusTo((previous) => ({ target, token: (previous?.token ?? 0) + 1 }));
  const options: PickOption[] = FIELD_LABELS.map(([key, label]) => ({ key, name: t(label) }));
  const send = () => {
    if (form === null || form.field === null || form.status === "sending" || sending.current) return;
    const note = form.note.trim();
    if (note === "") return;
    sending.current = true;
    const draft = form;
    setForm({ ...draft, status: "sending" });
    void control.send({ field: form.field, note }).then((done) => {
      sending.current = false;
      // 等回應時使用者可能已經按 Tab 走到別處：焦點還在表單裡（或掉到 body）才移回「回報錯誤」。
      const focusInForm = formRef.current?.contains(document.activeElement) === true || document.activeElement === document.body;
      setForm(done ? null : { ...draft, status: "failed" });
      if (done && focusInForm) moveFocus("open");
    });
  };
  const busy = form?.status === "sending";
  const canSend = form !== null && !busy && form.field !== null && form.note.trim() !== "";
  const open =
    form === null ? (
      <button
        ref={openRef}
        type="button"
        data-report-open=""
        onClick={() => {
          setForm({ field: null, note: "", status: "idle" });
          moveFocus("form");
        }}
        className={`${LINK_CLASS} text-nav`}
      >
        {t("paper.report.open")}
      </button>
    ) : null;
  return (
    <div data-summary-report="">
      {parts.heading(open)}
      {form === null ? null : (
        <div ref={formRef} data-report-form={form.status} className="mt-[20px] border-t border-hairline pt-6">
          <PickGroup
            kind="report"
            label={t("paper.report.field")}
            options={options}
            picked={form.field === null ? [] : [form.field]}
            locked={busy}
            onPick={(key) => setForm({ ...form, field: key })}
            multiple={false}
            name="field"
          />
          <div className="mt-6">
            <OneLineField
              kind="report"
              label={t("paper.report.note")}
              value={form.note}
              locked={busy}
              onChange={(note) => setForm({ ...form, note })}
            />
          </div>
          {form.status === "failed" ? <FailedLine kind="report">{t("paper.report.failed")}</FailedLine> : null}
          <SendActions
            kind="report"
            busy={busy}
            canSend={canSend}
            sendText={busy ? t("paper.report.sending") : t("paper.report.send")}
            cancelText={t("paper.report.cancel")}
            onSend={send}
            onCancel={() => {
              setForm(null);
              moveFocus("open");
            }}
          />
        </div>
      )}
      {parts.list}
      {parts.note}
    </div>
  );
}
