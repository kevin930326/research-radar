import { useId, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ClassificationFixBody, ClassificationFixEntryResponse, DisplayNames } from "../../generated/model";
import { taipeiDate } from "../taipei-date";
import { inTaxonomyOrder, type ClassificationRowParts } from "./PaperBiblio";
import { InfoGlyph, MONO_LABEL_CLASS } from "./parts";

// 修正分類（Story 3.11；EXPERIENCE.md 畫面 8 第 2 點、決定第 95～98 項；設計稿 paper.html 的「修正分類」六個狀態）。
// 成員版才有：只有 pages/PaperPage.tsx（成員版）import 這個檔，示範站的建置不會帶到（e2e/build-output.spec.ts 檢查
// `data-classification-fix`）。
// - 分類那一行：標籤、AI 標章、「有 N 位成員修正過[，意見不同]」（可以按，展開／收起歷史）、「修正分類」（表單開著時不顯示）。
// - 「修正分類」在那一行下面原地展開表單：主題與方法各一組可複選的標籤（照分類表的順序，目前的分類先選好）、
//   「理由」＋「選填」（100 字以內，接近上限出現「剩 N 字」）、「送出修正」「取消」、最下面一行「送出後不能撤回，改錯了再修正一次」。
//   兩組各至少選一個、而且和目前的分類不同，才能按「送出修正」。
// - 送出中：按鈕寫「送出中…」、標籤與理由不能改、「取消」不顯示。沒有成功：按鈕上方一行「沒有送出成功。你選的還在，再試一次」，
//   選的與寫的都留著。成功：表單收起來，分類那一行換成新的（外層換上 API 回的分類區塊：「◆ 已核對」＋人數）。
// - 歷史新的在上：日期（台北）、姓名、選的主題與方法（照分類表的順序），有寫理由的多一行；兩筆以上時第一筆標「目前採用」。
// 小的可點的東西（人數、「修正分類」、標籤）看起來照設計稿的高度；按鈕本身至少 44×44，用負外距讓那一行看起來一樣高（ADR-0037）。

/** 理由的上限（後端 REASON_MAX_LENGTH；決定第 98 項）。 */
export const REASON_MAX_LENGTH = 100;
/** 剩幾個字以內才出現「剩 N 字」（EXPERIENCE.md「文字輸入」：接近上限才出現；門檻沒有規定，列給 Kevin）。 */
export const REMAINING_HINT_FROM = 20;

/** 修正分類要的資料與動作：由成員版的路由（routes/classification-fix.ts）給。 */
export interface ClassificationFixControl {
  /** 送出：成功回 true（外層已經換上新的分類區塊），沒有成功回 false。不丟例外。 */
  send: (body: ClassificationFixBody) => Promise<boolean>;
  /** 歷史展開了沒有。 */
  historyOpen: boolean;
  onToggleHistory: () => void;
  /** 歷史（新的在前）；還沒讀到或沒讀到是 null（不顯示）。 */
  history: readonly ClassificationFixEntryResponse[] | null;
}

/**
 * 設計稿 button.lk（「有 N 位成員修正過」「修正分類」）：看起來 28px 高。按鈕本身 44×44（ADR-0037），上下各 -8px 的外距
 * 讓這一行看起來和設計稿一樣高。
 */
const LINK_CLASS =
  "-my-2 inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center gap-[7px] bg-transparent p-[0px] text-s13 tracking-004 whitespace-nowrap text-accent-gold decoration-1 underline-offset-4 hover:underline";

function Caret() {
  return (
    <svg width="9" height="6" viewBox="0 0 9 6" fill="none" aria-hidden="true" className="transition-transform duration-200">
      <path d="M1 1l3.5 3.5L8 1" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

/** 一組可複選的標籤（設計稿 button.pk：看起來 32px 高；按鈕本身 44px，上下各 -6px 的外距，兩排之間的 12px 剛好分給上下兩排）。 */
function PickGroup({
  label,
  options,
  picked,
  locked,
  onToggle,
  field,
}: {
  label: string;
  options: DisplayNames["topics"];
  picked: readonly string[];
  locked: boolean;
  onToggle: (key: string) => void;
  field: "topic" | "method";
}) {
  return (
    <div role="group" aria-label={label} data-fix-group={field}>
      <div className={MONO_LABEL_CLASS}>{label}</div>
      <div className="mt-3 flex flex-wrap gap-x-2 gap-y-3">
        {options.map((option) => {
          const on = picked.includes(option.key);
          return (
            <button
              key={option.key}
              type="button"
              aria-pressed={on}
              disabled={locked}
              data-fix-pick={option.key}
              onClick={() => onToggle(option.key)}
              className="group -my-[6px] inline-flex h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] disabled:cursor-default"
            >
              <span
                className={`inline-flex h-[32px] w-full items-center justify-center rounded-full border px-[14px] font-body text-s13 tracking-004 whitespace-nowrap transition-[border-color,color,background-color] duration-200 ${
                  on
                    ? "border-text-primary bg-text-primary text-on-button"
                    : "border-border bg-transparent text-text-reading group-enabled:group-hover:border-text-primary"
                }`}
              >
                {option.name}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function sameKeys(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && [...a].sort().join("\u0000") === [...b].sort().join("\u0000");
}

/** 目前的分類裡、分類表有的 key（「未分類」不是分類表的 key，不先選）。 */
function currentKeys(keys: readonly string[] | undefined, options: DisplayNames["topics"]): string[] {
  const known = new Set(options.map((option) => option.key));
  return (keys ?? []).filter((key) => known.has(key));
}

type FormState = { topic: string[]; method: string[]; reason: string; status: "idle" | "sending" | "failed" };

function FixForm({
  form,
  names,
  current,
  onChange,
  onSend,
  onCancel,
}: {
  form: FormState;
  names: DisplayNames;
  current: { topic: string[]; method: string[] };
  onChange: (next: FormState) => void;
  onSend: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("paper");
  const reasonId = useId();
  const remainingId = useId();
  const busy = form.status === "sending";
  const unchanged = sameKeys(form.topic, current.topic) && sameKeys(form.method, current.method);
  const canSend = !busy && form.topic.length > 0 && form.method.length > 0 && !unchanged;
  const toggle = (field: "topic" | "method", key: string) => {
    const list = form[field];
    onChange({ ...form, [field]: list.includes(key) ? list.filter((item) => item !== key) : [...list, key] });
  };
  const remaining = REASON_MAX_LENGTH - [...form.reason].length;
  return (
    <div data-fix-form={form.status} className="mt-6 border-t border-hairline pt-6">
      <PickGroup
        label={t("paper.classification.topic")}
        options={names.topics}
        picked={form.topic}
        locked={busy}
        onToggle={(key) => toggle("topic", key)}
        field="topic"
      />
      <div className="mt-6">
        <PickGroup
          label={t("paper.classification.method")}
          options={names.methods}
          picked={form.method}
          locked={busy}
          onToggle={(key) => toggle("method", key)}
          field="method"
        />
      </div>
      <div className="mt-6">
        <label htmlFor={reasonId} className={`block ${MONO_LABEL_CLASS}`}>
          {t("paper.fix.reason")}
          <span className="text-text-tertiary">{t("paper.fix.optional")}</span>
        </label>
        <input
          id={reasonId}
          type="text"
          maxLength={REASON_MAX_LENGTH}
          autoComplete="off"
          readOnly={busy}
          value={form.reason}
          aria-describedby={remaining <= REMAINING_HINT_FROM ? remainingId : undefined}
          onChange={(event) => onChange({ ...form, reason: event.target.value })}
          className="block h-tap-target w-full rounded-none border-0 border-b border-border bg-transparent p-[0px] font-body text-s16 tracking-001 text-text-primary transition-[border-color] duration-250 read-only:text-text-secondary focus:border-accent-gold"
        />
        {remaining <= REMAINING_HINT_FROM ? (
          <div id={remainingId} data-fix-remaining="" className="mt-2 font-mono text-s13 tracking-006 text-text-tertiary">
            {t("paper.fix.remaining", { n: remaining })}
          </div>
        ) : null}
      </div>
      {form.status === "failed" ? (
        <div role="alert" data-fix-failed="" className="mt-4 flex items-start gap-[9px] text-s14 leading-160 text-danger">
          <InfoGlyph className="mt-[5px] shrink-0" />
          <span>{t("paper.fix.failed")}</span>
        </div>
      ) : null}
      <div className="mt-6 flex items-center gap-6">
        <button
          type="button"
          data-fix-send=""
          disabled={!canSend}
          onClick={onSend}
          className="inline-flex h-tap-target min-w-[120px] cursor-pointer items-center justify-center rounded-full bg-text-primary px-[28px] font-body text-button text-s14 tracking-006 text-on-button transition-transform duration-250 enabled:hover:-translate-y-px disabled:cursor-default disabled:opacity-55"
        >
          {busy ? t("paper.fix.sending") : t("paper.fix.send")}
        </button>
        {busy ? null : (
          <button
            type="button"
            data-fix-cancel=""
            onClick={onCancel}
            className="inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] text-nav text-text-secondary transition-[color] duration-250 hover:text-text-primary"
          >
            {t("paper.fix.cancel")}
          </button>
        )}
      </div>
      <div className="mt-4 flex items-start gap-[9px] text-s13 leading-160 text-text-tertiary">
        <InfoGlyph className="mt-[3px] shrink-0" />
        <span>{t("paper.fix.hint")}</span>
      </div>
    </div>
  );
}

function History({ entries, names }: { entries: readonly ClassificationFixEntryResponse[]; names: DisplayNames }) {
  const { t } = useTranslation("paper");
  const topics = new Map(names.topics.map((entry) => [entry.key, entry.name]));
  const methods = new Map(names.methods.map((entry) => [entry.key, entry.name]));
  const separator = t("paper.biblio.authorSeparator");
  const list = (keys: readonly string[], map: Map<string, string>) =>
    inTaxonomyOrder(keys, map)
      .map((key) => map.get(key) ?? key)
      .join(separator);
  const mono = "flex flex-wrap gap-x-[22px] gap-y-[6px] font-mono text-s13 tracking-006 text-text-secondary";
  return (
    <div data-fix-history="" className="mt-4 border-t border-hairline">
      {entries.map((entry, index) => (
        <div key={`${entry.fixed_at}-${index}`} data-fix-entry="" className={`py-4 ${index > 0 ? "border-t border-hairline" : ""}`}>
          <div className={`${mono} items-center`}>
            <span>{taipeiDate(entry.fixed_at)}</span>
            <span className="text-text-primary">{entry.member_name}</span>
            {index === 0 && entries.length > 1 ? <span>{t("paper.fix.current")}</span> : null}
          </div>
          <div className={`mt-2 leading-160 ${mono}`}>
            {entry.topic === null ? null : (
              <span>
                {t("paper.classification.topic")} <span className="text-text-primary">{list(entry.topic, topics)}</span>
              </span>
            )}
            {entry.method === null ? null : (
              <span>
                {t("paper.classification.method")} <span className="text-text-primary">{list(entry.method, methods)}</span>
              </span>
            )}
          </div>
          {entry.reason === null ? null : (
            <div className="mt-3 border-l border-border pl-4 text-s15 leading-170 text-text-secondary">{entry.reason}</div>
          )}
        </div>
      ))}
    </div>
  );
}

/** 分類那一塊（成員版）：見檔頭。 */
export function ClassificationFixArea({ parts, control }: { parts: ClassificationRowParts; control: ClassificationFixControl }) {
  const { t } = useTranslation("paper");
  const [form, setForm] = useState<FormState | null>(null);
  const { names, block } = parts;
  const current = {
    topic: currentKeys(block.value?.topic?.keys, names.topics),
    method: currentKeys(block.value?.method?.keys, names.methods),
  };
  const send = () => {
    if (form === null || form.status === "sending") return;
    const draft = form;
    setForm({ ...draft, status: "sending" });
    const reason = draft.reason.trim();
    void control
      .send({ topic: draft.topic, method: draft.method, ...(reason === "" ? {} : { reason }) })
      .then((done) => setForm(done ? null : { ...draft, status: "failed" }));
  };
  let row: ReactNode = parts.tags;
  if (parts.countLabel !== null) {
    row = (
      <>
        {row}
        <button
          type="button"
          data-fix-count=""
          aria-expanded={control.historyOpen}
          onClick={control.onToggleHistory}
          className={`${LINK_CLASS} [&[aria-expanded=true]_svg]:rotate-180`}
        >
          {parts.countLabel}
          <Caret />
        </button>
      </>
    );
  }
  return (
    <div data-classification-fix="">
      <div className={parts.rowClassName}>
        {row}
        {form === null ? (
          <button
            type="button"
            data-fix-open=""
            onClick={() => setForm({ ...current, reason: "", status: "idle" })}
            className={LINK_CLASS}
          >
            {t("paper.fix.open")}
          </button>
        ) : null}
      </div>
      {parts.titleOnly}
      {control.historyOpen && control.history !== null && control.history.length > 0 ? (
        <History entries={control.history} names={names} />
      ) : null}
      {form === null ? null : (
        <FixForm form={form} names={names} current={current} onChange={setForm} onSend={send} onCancel={() => setForm(null)} />
      )}
    </div>
  );
}
