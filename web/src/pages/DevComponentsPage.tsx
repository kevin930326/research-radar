import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { AiBadge } from "../components/AiBadge";
import { Button } from "../components/Button";
import { PaperAbstract, PaperTitle } from "../components/PaperText";
import { StatusTag } from "../components/StatusTag";
import { ExternalLink, TextLink } from "../components/TextLink";
import { useReducedMotion } from "../hooks/use-reduced-motion";
import i18n, { LANGUAGE } from "../i18n";
import dev from "../i18n/zh-TW/dev.json";
import {
  COLOR_SWATCHES,
  EXTERNAL_EXAMPLE,
  FONT_SIZE_SCALE,
  LETTER_SPACING_SCALE,
  LINE_HEIGHT_SCALE,
  PAPER_TITLE_TOKENS,
  TYPE_SCALE,
  UNVERIFIED,
  VERIFIED,
  colorToken,
  scaleToken,
  typeToken,
} from "./dev-showcase";

// 元件示範頁（/dev/components）：只在開發環境掛路由（routes/member.tsx），正式建置沒有這一頁。
// 示範頁自己的標籤（標題與六個區塊名稱）放在 dev.json，只在這裡載入，不進正式的 i18n resources；
// 按鈕與狀態標記的範例字取 EXPERIENCE.md 既有的說法。這裡的元件就是各畫面用的同一份。
i18n.addResourceBundle(LANGUAGE, "dev", dev, true, true);

const SECTION_CLASS = "flex flex-col gap-6 border-t border-hairline py-12";
const HEADING_CLASS = "font-title-zh text-section-title-zh text-text-primary";
const TOKEN_CLASS = "font-mono text-label-mono text-text-tertiary";
// 有說明框的那一排下面多留 64px（spacing-16）：說明框往下展開（約 90px）時不壓到下一段的分隔線與標題。
const POPOVER_ROW_CLASS = "flex flex-wrap items-center gap-6 pb-16";

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={`dev-${id}`} data-dev-section={id} className={SECTION_CLASS}>
      <h2 id={`dev-${id}`} className={HEADING_CLASS}>
        {title}
      </h2>
      {children}
    </section>
  );
}

function TypeRow({ className }: { className: string }) {
  const { t } = useTranslation("notice");
  const token = typeToken(className);
  const paperSize = PAPER_TITLE_TOKENS[token];
  let sample: ReactNode;
  if (paperSize !== undefined) {
    sample = <PaperTitle size={paperSize}>{token}</PaperTitle>;
  } else if (token === "reading-en") {
    sample = <PaperAbstract>{token}</PaperAbstract>;
  } else {
    // 中文字體的代幣多放一段中文，看得到中文字的樣子（用正式畫面既有的字）。
    const zh = className.includes("font-title-zh") || className.includes("font-body");
    sample = (
      <p className={`${className} text-text-primary`}>
        {token}
        {zh ? ` ${t("notice.page.title")}` : null}
      </p>
    );
  }
  return (
    <li data-type-token={token} className="flex flex-col gap-1 border-t border-hairline py-4">
      <span className={TOKEN_CLASS}>{token}</span>
      <div className="break-words">{sample}</div>
    </li>
  );
}

/** 一組尺度（ADR-0038）：每個值一個樣本，樣本的字就是名稱，套上那個類別。 */
function ScaleList({ scale, classes }: { scale: string; classes: readonly string[] }) {
  return (
    <ul data-scale={scale} className="flex flex-wrap items-baseline gap-x-6 gap-y-3 border-t border-hairline py-4">
      {classes.map((className) => (
        <li key={className} data-scale-token={scaleToken(className)} className={`${className} text-text-primary`}>
          {scaleToken(className)}
        </li>
      ))}
    </ul>
  );
}

export function DevComponentsPage() {
  const { t } = useTranslation(["dev", "notice"]);
  const reducedMotion = useReducedMotion();
  return (
    <article data-reduced-motion={String(reducedMotion)} className="max-w-reading-width">
      <h1 className="pb-12 font-title-zh text-page-title-zh text-text-primary">{t("dev:dev.page.title")}</h1>

      <Section id="colors" title={t("dev:dev.sections.colors")}>
        <ul className="grid grid-cols-2 gap-4 tablet:grid-cols-3">
          {COLOR_SWATCHES.map((className) => (
            <li key={className} data-color-token={colorToken(className)} className="flex items-center gap-3">
              <span aria-hidden="true" className={`h-12 w-12 shrink-0 border border-hairline ${className}`} />
              <span className={`${TOKEN_CLASS} min-w-[0] break-words`}>{colorToken(className)}</span>
            </li>
          ))}
        </ul>
        <ul>
          {TYPE_SCALE.map((className) => (
            <TypeRow key={className} className={className} />
          ))}
        </ul>
        <ScaleList scale="sizes" classes={FONT_SIZE_SCALE} />
        <ScaleList scale="leading" classes={LINE_HEIGHT_SCALE} />
        <ScaleList scale="tracking" classes={LETTER_SPACING_SCALE} />
      </Section>

      <Section id="buttons" title={t("dev:dev.sections.buttons")}>
        <div className="flex flex-wrap items-center gap-4">
          <Button variant="primary" arrow>
            {t("dev:dev.examples.primary")}
          </Button>
          <Button variant="secondary">{t("dev:dev.examples.secondary")}</Button>
          <Button variant="primary" disabled>
            {t("dev:dev.examples.pending")}
          </Button>
        </div>
      </Section>

      <Section id="links" title={t("dev:dev.sections.links")}>
        <div className="flex flex-wrap items-center gap-6">
          <TextLink to="/notice">{t("notice:notice.page.title")}</TextLink>
          <ExternalLink href={EXTERNAL_EXAMPLE.href}>{EXTERNAL_EXAMPLE.label}</ExternalLink>
        </div>
      </Section>

      <Section id="ai-badges" title={t("dev:dev.sections.aiBadges")}>
        <div className={POPOVER_ROW_CLASS}>
          <AiBadge provenance={UNVERIFIED} first />
          <AiBadge provenance={UNVERIFIED} />
          <AiBadge provenance={VERIFIED} />
        </div>
      </Section>

      <Section id="status-tags" title={t("dev:dev.sections.statusTags")}>
        <div className={POPOVER_ROW_CLASS}>
          <StatusTag info={t("dev:dev.examples.outOfScopeInfo")}>{t("dev:dev.examples.outOfScope")}</StatusTag>
          <StatusTag>{t("dev:dev.examples.private")}</StatusTag>
        </div>
      </Section>

      <Section id="focus" title={t("dev:dev.sections.focus")}>
        <div className="flex flex-wrap items-center gap-6 p-2">
          {/* 外框樣本：和鍵盤焦點同一條 CSS 規則（styles.css 的 [data-focus-sample]），不在 Tab 順序裡。 */}
          <Button variant="secondary" tabIndex={-1} data-focus-sample>
            {t("dev:dev.examples.secondary")}
          </Button>
        </div>
      </Section>
    </article>
  );
}
