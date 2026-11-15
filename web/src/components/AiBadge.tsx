import { useTranslation } from "react-i18next";

import { InfoPopover } from "./InfoPopover";
import { taipeiDate } from "./taipei-date";

// AI 標章（DESIGN.md Components「AI 標章」、EXPERIENCE.md Component Patterns「AI 標章」、ADR-0010 第 8 點）：
// - 未經人工核對：空心菱形 ◇＋「AI 整理」；頁面上第一個未核對的標章（`first`）寫全文「AI 生成・未經人工核對」。
// - 人工核對過：實心菱形 ◆＋「已核對」，在哪裡、是不是第一個都一樣。
// - 可點，說明框是完整說法（「AI 生成・未經人工核對」或「AI 生成・已核對」）、模型名稱、產生日期（台北時間）。
// - 資料只來自 provenance；沒有 provenance 就什麼都不顯示。
// DM Mono 小字（label-mono）、第二層文字色。

/**
 * AI 產出的來歷。暫定的前端形狀：後端的共用模型（radar/domain/contracts.py）由第一個用到它的 Story
 * （3.6 論文頁的 API）定義，型別改由 openapi.json 產生後，這裡換成產生的型別。
 */
export interface Provenance {
  /** 模型名稱。 */
  model: string;
  /** 產生時間（ISO 8601 UTC）。 */
  generatedAt: string;
  /** 是否經人工核對（v1 只有分類與構面合併會是 true）。 */
  verified: boolean;
}

const OPEN_DIAMOND = "◇";
const SOLID_DIAMOND = "◆";

interface AiBadgeProps {
  provenance: Provenance | null | undefined;
  /** 這一頁第一個未核對的標章：寫全文。 */
  first?: boolean;
}

/** 說明框的內容：完整說法、模型名稱、產生日期（台北時間）。 */
export function ProvenanceDetails({ full, provenance }: { full: string; provenance: Provenance }) {
  // 產生時間是 API 來的資料：看不懂就不顯示日期，其他照常顯示。
  const date = taipeiDate(provenance.generatedAt);
  return (
    <span className="flex flex-col">
      <span>{full}</span>
      <span data-provenance="model">{provenance.model}</span>
      {date === null ? null : <span data-provenance="date">{date}</span>}
    </span>
  );
}

export function AiBadge({ provenance, first = false }: AiBadgeProps) {
  const { t } = useTranslation("ai");
  if (provenance === null || provenance === undefined) {
    return null;
  }
  const { verified } = provenance;
  const mark = verified ? SOLID_DIAMOND : OPEN_DIAMOND;
  const label = verified ? t("ai.badge.verified") : first ? t("ai.badge.full") : t("ai.badge.short");
  const full = verified ? t("ai.badge.verifiedFull") : t("ai.badge.full");
  return (
    <InfoPopover
      trigger={
        <span
          data-ai-badge={verified ? "verified" : "unverified"}
          className="inline-flex items-center gap-1 font-mono text-label-mono text-text-secondary"
        >
          <span aria-hidden="true">{mark}</span>
          <span>{label}</span>
        </span>
      }
    >
      <ProvenanceDetails full={full} provenance={provenance} />
    </InfoPopover>
  );
}
