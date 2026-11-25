import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DataDateBar } from "../components/DataDateBar";
import { ErrorBar } from "../components/search/ListParts";
import type { DisplayNames, SiteKind } from "../datasource/types";
import type { CubeRow, SnapshotRef, TrendParams } from "../generated/model";
import { coverageTable, type CoverageCell, type CoverageTable } from "../stats/coverage";
import { formatPercent } from "../stats/format";
import { formatCount } from "./paper-format";

// 資料涵蓋範圍（Story 3.15；EXPERIENCE.md 畫面 9、State Patterns「9. 資料涵蓋範圍」）。樣子照設計稿
// docs/ux/reference/screens/coverage.html（成員版與示範站版，各有正常／載入中／錯誤，桌機與手機 390）。五個部分：
// 1. 一段白話說明（追蹤幾本、從哪一年起、資料來源與更新頻率）。
// 2. 三個讀數：收錄、有摘要（篇數與占比）、第三個成員版是「本週新增」、示範站是「有完整內容」。
//    數字是連結的照設計稿：成員版只有本週新增（到找論文並帶入「本週新增」；找論文沒有「有摘要」的條件），
//    示範站三個都是（到論文清單帶入條件）。數字是 0 時不是連結（EXPERIENCE.md「讀數」）。
// 3. 摘要覆蓋率表：期刊 × 追蹤起始年到當年度；比例由 web/src/stats/ 算（AD-19），顯示位數由 formatPercent 決定。
//    低於門檻（params，預設 50%）的格子是金色空心點；那一年沒有論文的格子是「—」，不是 0%，也不套金色。
//    當年度那一欄的欄名加註「未滿一年」。指到（手機是點、鍵盤是 Tab 到）一格，表格右上（手機在標題下）顯示那一格的
//    篇數；報讀器只在點或 Tab 到時唸（指到不唸，免得滑過去一直唸）。格子可以聚焦，外框是全站的金色焦點外框（ADR-0037）。
//    計數壞掉（有摘要比總數多）時算不出表格：整頁當成「沒有載入成功」，不讓整頁崩掉。
//    899px 以下表格固定 866px 寬、左右捲動，第一欄（期刊）固定在左邊（Responsive & Platform）。
// 4. Elsevier 四本的說明（設計稿的三句，寫死）。
// 5. 名詞說明：追蹤範圍、追蹤範圍外、有摘要、快照編號。
// 狀態：載入中是讀數與表格的骨架（沒有圖例與 Elsevier 的說明）；錯誤是全站共用的「沒有載入成功」（ListParts 的
// ErrorBar），下面接名詞說明。「空的」不會發生（有資料才會產生快照；成員版上線前已完成第一次匯入）：
// 沒有任何列時照樣畫表格，每一格都是「—」。
// 說明與名詞說明裡的「14 本」「2016 年」照設計稿寫在 i18n（coverage-text.test.ts 檢查和設定一致）；名詞說明「快照編號」的
// 例子用這一次的快照編號（設計稿寫 #12；還不知道快照時照設計稿的 12）。

/**
 * 期刊的縮寫（設計稿 coverage.html 與 EXPERIENCE.md 畫面 9 的寫法；I&M、I&O 不是代號轉大寫）。
 * coverage-text.test.ts 檢查 config/journals.yaml 的每個代號都在這裡。
 */
export const JOURNAL_ABBREVIATIONS: Readonly<Record<string, string>> = {
  misq: "MISQ",
  isr: "ISR",
  jmis: "JMIS",
  jais: "JAIS",
  ejis: "EJIS",
  isj: "ISJ",
  jit: "JIT",
  jsis: "JSIS",
  dss: "DSS",
  im: "I&M",
  io: "I&O",
  pmj: "PMJ",
  tem: "TEM",
  database: "DATABASE",
};

/** 設計稿的「快照編號」例子用的號碼（還不知道這一次的快照時）。 */
const EXAMPLE_SNAPSHOT_NUMBER = 12;

/** 期刊的縮寫：對照表裡有的照表；沒有的（測試用的假期刊）用代號轉大寫。 */
export function journalAbbreviation(key: string): string {
  return JOURNAL_ABBREVIATIONS[key] ?? key.toUpperCase();
}

/** 算覆蓋率表；計數壞掉（coverageTable 丟 RangeError）時回 null，畫面當成「沒有載入成功」。 */
function safeCoverageTable(data: CoverageData): CoverageTable | null {
  try {
    return coverageTable(data.rows, data.params, data.names.journals.map((journal) => journal.key), data.snapshot.as_of);
  } catch (error) {
    if (error instanceof RangeError) return null;
    throw error;
  }
}

export interface CoverageData {
  rows: readonly CubeRow[];
  params: TrendParams;
  names: DisplayNames;
  snapshot: SnapshotRef;
  /** 第三個讀數的篇數。 */
  third: number;
}

/** 讀數數字的連結（成員版、示範站各自決定連到哪裡）：沒給的不是連結；數字是 0 時也不做成連結。 */
export type ReadingLinks = Partial<Record<"total" | "withAbstract" | "third", (children: ReactNode) => ReactNode>>;

export interface CoveragePageProps {
  site: SiteKind;
  state: "loading" | "error" | "ready";
  /** 資料日期列用的快照（還不知道時是 null：整列不顯示）。 */
  snapshot: SnapshotRef | null;
  data: CoverageData | null;
  links: ReadingLinks;
  onRetry: () => void;
}

// 表頭與每列的第一格是 <th>（瀏覽器預設粗體）：字級用帶字重 400 的角色類別（text-caption 等），不另寫字重。
const H2_CLASS = "font-title-zh text-section-title-zh text-text-primary max-split:text-s21";
const READING_LABEL_CLASS = "text-s14 tracking-006 text-text-secondary";
const UNIT_CLASS = "pl-[8px] font-body text-s14 tracking-004 text-text-secondary";
const BAR_CLASS = "max-w-full bg-hairline";
/**
 * 數字連結：平常和一般數字一樣，滑過或聚焦才出現底線（EXPERIENCE.md「讀數」）。可點範圍至少 44×44（ADR-0037）：
 * 行內元素，上下內距不影響排版；左右內距用一樣大的負外距抵掉，數字位置不動（一位數的數字也夠寬）。
 */
export const NUMBER_LINK_CLASS = "-mx-[12px] px-[12px] py-[4px] text-text-primary no-underline decoration-1 underline-offset-[6px] hover:underline";

export function CoveragePage({ site, state, snapshot, data, links, onRetry }: CoveragePageProps) {
  const { t } = useTranslation("coverage");
  const { t: tc } = useTranslation("common");
  const thirdLabel = site === "member" ? t("coverage.readings.thisWeek") : t("coverage.readings.fullContent");
  const table = state === "ready" && data !== null ? safeCoverageTable(data) : null;
  const shown = state === "ready" && table === null ? "error" : state;
  const ready = shown === "ready" && data !== null;
  return (
    <div data-page="coverage" data-state={shown} className="text-s14 leading-normal tracking-0 text-text-primary">
      <DataDateBar snapshot={snapshot} />
      <h1 className="mt-[14px] font-title-zh text-page-title-zh text-balance text-text-primary split:mt-[18px]">
        {t("coverage.page.title")}
      </h1>
      <p className="mt-[20px] max-w-reading-width text-s16 leading-190 tracking-003 text-pretty text-text-reading split:mt-[28px] split:text-s17">
        {t("coverage.intro.text")}
      </p>
      {shown === "error" ? (
        <ErrorBar message={tc("states.loadFailed.message")} action={tc("states.loadFailed.action")} onRetry={onRetry} />
      ) : (
        <>
          <Readings
            labels={[t("coverage.readings.total"), t("coverage.readings.withAbstract"), thirdLabel]}
            unit={t("coverage.readings.unit")}
            table={table}
            third={ready ? data.third : null}
            links={links}
          />
          {table !== null && data !== null ? <CoverageGrid table={table} names={data.names} params={data.params} /> : <GridSkeleton title={t("coverage.table.title")} />}
          {table !== null ? <Elsevier /> : null}
        </>
      )}
      <Terms snapshotNumber={snapshot?.number ?? EXAMPLE_SNAPSHOT_NUMBER} />
    </div>
  );
}

function Readings({
  labels,
  unit,
  table,
  third,
  links,
}: {
  labels: [string, string, string];
  unit: string;
  table: CoverageTable | null;
  third: number | null;
  links: ReadingLinks;
}) {
  const values: [number, number, number] | null = table === null || third === null ? null : [table.total, table.withAbstract, third];
  const keys = ["total", "withAbstract", "third"] as const;
  const percent = table === null ? null : formatPercent(table.ratio);
  return (
    <dl data-readings className="mt-[28px] border-y border-hairline split:mt-[44px] split:flex split:max-w-reading-width split:flex-wrap split:gap-y-[16px] split:border-y-0">
      {labels.map((label, at) => {
        const value = values?.[at] ?? null;
        const link = links[keys[at]!];
        const number = value === null ? null : formatCount(value);
        return (
          <div
            key={label}
            data-reading={keys[at]}
            className={`flex items-baseline justify-between gap-[16px] py-[16px] split:block split:min-w-[160px] split:flex-1 split:pt-[2px] split:pb-[8px] ${
              at > 0 ? "border-t border-hairline split:border-t-0 split:border-l split:pl-[28px]" : ""
            }`}
          >
            <dt className={READING_LABEL_CLASS}>{label}</dt>
            {number === null ? (
              <dd aria-hidden="true" className="flex">
                <span className={`${BAR_CLASS} h-[22px] w-[96px] split:mt-[14px] split:mb-[6px] split:h-[30px] split:w-[150px]`} />
              </dd>
            ) : (
              <dd className="font-mono text-s28 leading-100 tracking-n001 text-text-primary split:mt-[14px] split:text-s36">
                {link !== undefined && value !== 0 ? link(<span data-reading-number>{number}</span>) : <span data-reading-number>{number}</span>}
                <span className={UNIT_CLASS}>{unit}</span>
                {at === 1 && percent !== null ? (
                  <span data-reading-percent className="pl-[14px] font-mono text-s14 tracking-004 text-text-secondary">
                    {percent}
                  </span>
                ) : null}
              </dd>
            )}
          </div>
        );
      })}
    </dl>
  );
}

function CellMark({ cell }: { cell: CoverageCell }) {
  if (cell.ratio === null) return <span aria-hidden="true" className="inline-block w-[15px]" />;
  return (
    <span
      aria-hidden="true"
      className={`mr-[8px] inline-block size-[7px] rounded-full ${cell.low ? "border border-accent-gold" : "bg-text-reading"}`}
    />
  );
}

interface Picked {
  journal: string;
  cell: CoverageCell;
}

function CoverageGrid({ table, names, params }: { table: CoverageTable; names: DisplayNames; params: TrendParams }) {
  const { t } = useTranslation("coverage");
  const [picked, setPicked] = useState<Picked | null>(null);
  // 報讀器唸的那一格：只在點或 Tab 到時換（指到只換畫面上的字）。
  const [announced, setAnnounced] = useState<Picked | null>(null);
  const choose = (next: Picked, announce: boolean) => {
    setPicked(next);
    if (announce) setAnnounced(next);
  };
  const describe = (item: Picked) =>
    `${t("coverage.pick.cell", { journal: journalAbbreviation(item.journal), year: item.cell.year })} ${
      item.cell.total === 0
        ? t("coverage.legend.empty")
        : t("coverage.pick.counts", { total: formatCount(item.cell.total), withAbstract: formatCount(item.cell.withAbstract) })
    }`;
  const threshold = formatPercent(params.low_coverage_threshold) ?? "";
  const pick =
    picked === null ? (
      <span className="font-body">
        <span className="hidden split:inline">{t("coverage.table.pickPointer")}</span>
        <span className="split:hidden">{t("coverage.table.pickTouch")}</span>
      </span>
    ) : (
      <span className="text-text-reading">
        {t("coverage.pick.cell", { journal: journalAbbreviation(picked.journal), year: picked.cell.year })}
        <span className="pl-[1em] font-body">
          {picked.cell.total === 0
            ? t("coverage.legend.empty")
            : t("coverage.pick.counts", { total: formatCount(picked.cell.total), withAbstract: formatCount(picked.cell.withAbstract) })}
        </span>
      </span>
    );
  return (
    <section aria-labelledby="coverage-table-title">
      <div className="mt-[48px] split:mt-[72px] split:flex split:items-baseline split:justify-between split:gap-[24px]">
        <h2 id="coverage-table-title" className={H2_CLASS}>
          {t("coverage.table.title")}
        </h2>
        <div
          data-coverage-pick
          className="mt-[10px] min-h-[22px] font-mono text-s13 tracking-004 whitespace-nowrap text-text-tertiary split:mt-[0px] split:min-h-[0px]"
        >
          {pick}
        </div>
        <span role="status" data-coverage-announce className="sr-only">
          {announced === null ? "" : describe(announced)}
        </span>
      </div>
      <div data-coverage-scroll className="mt-[20px] overflow-x-auto border-b border-hairline split:mt-[24px]">
        <table className="w-[866px] table-fixed border-collapse split:w-full split:min-w-[900px]">
          <colgroup>
            <col className="w-[96px] split:w-auto" />
            {table.years.map((year) => (
              <col key={year} className="w-[70px]" />
            ))}
          </colgroup>
          <thead>
            <tr>
              <th
                scope="col"
                className="sticky left-[0px] z-[1] bg-background pb-[12px] text-left align-bottom font-body text-caption whitespace-nowrap text-text-secondary"
              >
                {/* 字包在 span 裡：和年份欄一樣貼著表頭底部（年份欄最後一欄多一行「未滿一年」）。 */}
                <span>{t("coverage.table.journal")}</span>
              </th>
              {table.years.map((year) => (
                <th
                  key={year}
                  scope="col"
                  className="pb-[12px] pl-[14px] text-left align-bottom font-mono text-label-mono-reading whitespace-nowrap text-text-secondary split:pl-[12px]"
                >
                  {year === table.currentYear ? (
                    <span data-partial-year className="mb-[4px] block font-body text-label-mono tracking-004">
                      {t("coverage.table.partialYear")}
                    </span>
                  ) : null}
                  {year}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row) => {
              const name = names.journals.find((journal) => journal.key === row.journal)?.name ?? row.journal;
              return (
                <tr key={row.journal} data-journal={row.journal}>
                  <th
                    scope="row"
                    className="sticky left-[0px] z-[1] h-[44px] max-w-[96px] border-t border-hairline bg-background pr-[8px] text-left split:h-[46px] split:max-w-none"
                  >
                    <span className="flex min-w-[0px] items-center">
                      <abbr title={name} className="w-[84px] flex-none font-mono text-caption tracking-002 text-text-primary no-underline split:w-[88px]">
                        {journalAbbreviation(row.journal)}
                      </abbr>
                      <span className="hidden min-w-[0px] truncate text-caption tracking-0 text-text-secondary desktop:inline">{name}</span>
                    </span>
                  </th>
                  {row.cells.map((cell) => {
                    const percent = formatPercent(cell.ratio);
                    const on = picked !== null && picked.journal === row.journal && picked.cell.year === cell.year;
                    return (
                      <td
                        key={cell.year}
                        data-year={cell.year}
                        data-state={cell.ratio === null ? "empty" : cell.low ? "low" : "ok"}
                        tabIndex={0}
                        onMouseEnter={() => choose({ journal: row.journal, cell }, false)}
                        onClick={() => choose({ journal: row.journal, cell }, true)}
                        onFocus={() => choose({ journal: row.journal, cell }, true)}
                        className={`h-[44px] border-t border-hairline py-[10px] pl-[14px] text-left font-mono text-s13 whitespace-nowrap transition-[background-color] duration-150 split:h-[46px] split:py-[0px] split:pl-[12px] split:text-s14 ${
                          cell.ratio === null ? "text-text-tertiary" : cell.low ? "text-accent-gold" : "text-text-reading"
                        } ${on ? "bg-menu-item-hover" : ""}`}
                      >
                        <CellMark cell={cell} />
                        {percent ?? "—"}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div data-coverage-legend className="mt-[14px] flex flex-wrap gap-x-[28px] gap-y-[6px] text-s13 leading-170 tracking-004 text-text-secondary">
        <span className="inline-flex items-center gap-[8px] whitespace-nowrap">
          <span aria-hidden="true" className="size-[7px] rounded-full bg-text-reading" />
          {t("coverage.legend.full", { percent: threshold })}
        </span>
        <span className="inline-flex items-center gap-[8px] whitespace-nowrap">
          <span aria-hidden="true" className="size-[7px] rounded-full border border-accent-gold" />
          <span className="text-accent-gold">{t("coverage.legend.low", { percent: threshold })}</span>
        </span>
        <span className="inline-flex items-center gap-[8px] whitespace-nowrap">
          <span aria-hidden="true" className="text-text-tertiary">
            —
          </span>
          {t("coverage.legend.empty")}
        </span>
      </div>
    </section>
  );
}

/** 載入中的表格骨架（設計稿：桌機 14 列、每列期刊一條加 11 格；手機 14 列、期刊一條加 4 格）。每列高 46／44 再加上框線 1px（設計稿的列高不含框線）。 */
function GridSkeleton({ title }: { title: string }) {
  const rows = Array.from({ length: 14 }, (_, at) => at);
  return (
    <section aria-busy="true">
      <div className="mt-[48px] split:mt-[72px] split:flex">
        <h2 className={H2_CLASS}>{title}</h2>
      </div>
      <div aria-hidden="true" data-coverage-skeleton className="mt-[20px] overflow-hidden border-b border-hairline pt-[30px] split:mt-[24px]">
        {rows.map((row) => (
          <div key={row} className="flex h-[45px] items-center gap-[36px] border-t border-hairline split:h-[47px] split:gap-[0px]">
            <div className="flex-none split:min-w-[0px] split:flex-1 split:pr-[24px]">
              <div className={`${BAR_CLASS} h-[10px] w-[60px] split:w-[210px]`} />
            </div>
            {Array.from({ length: 11 }, (_, at) => (
              <div key={at} className={`split:w-[70px] split:flex-none ${at >= 4 ? "hidden split:block" : ""}`}>
                <div className={`${BAR_CLASS} h-[10px] w-[34px]`} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

function Elsevier() {
  const { t } = useTranslation("coverage");
  const text = "m-[0px] text-s15 leading-190 tracking-002 text-pretty";
  return (
    <div data-coverage-elsevier className="mt-[28px] flex max-w-reading-width flex-col gap-[8px] split:mt-[36px]">
      <p className={`${text} text-text-reading`}>{t("coverage.elsevier.journals", { journals: t("coverage.elsevier.list") })}</p>
      <p className={`${text} text-text-reading`}>{t("coverage.elsevier.effect")}</p>
      <p className={`${text} text-text-primary`}>{t("coverage.elsevier.note")}</p>
    </div>
  );
}

function Terms({ snapshotNumber }: { snapshotNumber: number }) {
  const { t } = useTranslation("coverage");
  const items = [
    ["scope", "scopeText"],
    ["outside", "outsideText"],
    ["abstract", "abstractText"],
    ["snapshot", "snapshotText"],
  ] as const;
  return (
    <section aria-labelledby="coverage-terms-title">
      <h2 id="coverage-terms-title" className={`mt-[56px] split:mt-[80px] ${H2_CLASS}`}>
        {t("coverage.terms.title")}
      </h2>
      <dl data-coverage-terms className="mt-[16px] max-w-reading-width border-y border-hairline split:mt-[20px]">
        {items.map(([term, text], at) => (
          <div key={term} className={`py-[16px] split:flex split:gap-[32px] split:py-[18px] ${at > 0 ? "border-t border-hairline" : ""}`}>
            <dt className="text-s15 tracking-002 text-text-primary split:w-[160px] split:flex-none split:leading-185">{t(`coverage.terms.${term}`)}</dt>
            <dd className="mt-[6px] text-s14 leading-185 text-text-secondary split:mt-[0px] split:text-s15 split:tracking-002">{t(`coverage.terms.${text}`, { number: snapshotNumber })}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
