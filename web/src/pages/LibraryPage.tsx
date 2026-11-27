import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { NEW_TAB_NOTE_ID, NewTabNote } from "../components/ExternalArrow";
import { ErrorBar, More } from "../components/search/ListParts";
import { Meta, OpenFullText, RemoveText, RowTitle } from "../components/search/PaperRows";
import { NoticeIcon } from "../components/search/parts";
import { DESKTOP_QUERY } from "../hooks/use-close-across-breakpoint";
import type { LibraryItem } from "../datasource/types";
import { useLibraryChange, type LibraryAction } from "../routes/library-change";
import { clientConfigQuery, displayNamesQuery, libraryListKey, libraryListQuery } from "../routes/paper-queries";
import { externalUrl, formatCount, splitTemplate } from "./paper-format";

// 我的文獻庫（Story 3.17；EXPERIENCE.md 畫面 13、State Patterns「13. 我的文獻庫」；設計稿 docs/ux/reference/screens/library.html）。
// 照 AR-67 的基本版：標題、篇數、論文列（勾選框、標題、期刊 · 年份、「移出」＋「開啟全文」）、勾選後的選取列、再載入。
// 2026-11-20 第二版（EXPERIENCE.md「Kevin 的決定」第 86～91 項；設計稿 library.html 第二版）：每列自己有「開啟全文」（不用勾；
// 沒有 DOI 的沒有）；勾選框平常暗一階；選取列是「已選 N 篇」＋「開啟全文」＋「取消」；批次開啟的結果與說明桌機跟著選取列
// 黏在上緣，手機在底部選取列的正上方。
// 不做（AR-67，Epic 6）：右欄（上傳或匯入、儲存空間）、「怎麼加入的」篩選、「有我的 PDF」「追蹤範圍外」兩個開關、
// 「有 N 篇等你確認」、「● 有我的 PDF」、不能直接移出的原因；每列第三行「AI 整理」不做（D1，Kevin 2026-11-20 決定）。
// - 一次列 client-config 的 library_page_size 篇（50）；「再載入 N 篇」「已顯示 n／N」，剩下不到一頁時寫實際的篇數，
//   全部列出來以後不顯示。目前顯示幾篇寫進網址（shown，AD-21）。
// - 勾選後：900px 以上，寫篇數的那一行原地換成選取列（黏在視窗上緣）；899px 以下選取列固定在畫面底部（寫篇數那一行照留著）。
//   「開啟全文」：一次最多 10 篇（超過時停用，旁邊「最多 10 篇」）；每篇開一個新分頁（DOI 連結），
//   沒有 DOI 的跳過（「N 篇沒有全文連結，已跳過」）；開了之後按鈕原地顯示「已開啟 N 篇」2 秒；
//   瀏覽器擋住新分頁時不顯示這一句，改在選取列下方列出連結。手機一次開不了多個分頁：在底部選取列上方列出連結。
//   瀏覽器擋住時列出的是還沒開出來的那幾篇（已經開了的不重複列）。
//   連結只開後端給的 DOI 連結（externalUrl 只收 http／https），不組學校代理或付費牆的網址。
// - 「移出」：那一列原地變成「已移出」＋「復原」，不跳確認；離開這一頁後消失（離開時丟掉文獻庫清單的快取）。
//   後端回 404（已經不在，例如另一個分頁移出了）時只顯示「已移出」、不給「復原」：這一次沒有真的移出，
//   復原會拿到別次移出的封存（ADR-0049 復原取最新一筆；審查 F3）。
// - 空的：「還沒有論文」＋一行「在論文頁按『加入我的文獻庫』」（AR-67 過渡做法，Kevin 2026-11-14 決定；
//   上傳、匯入兩行與「上傳或匯入」按鈕是 Epic 6）。
// - 載入中：論文列的骨架；錯誤：「沒有載入成功」＋「重新整理」（照設計稿的字，列給 Kevin 決定）。
// - 任何一個 API 回 401：照登入過期處理（路由清快取、回登入頁）。
// 頁面最外層把字級、行高、字距設成設計稿 .pp 的樣子，區塊只寫不一樣的地方。

/** 一次最多開幾篇全文（EXPERIENCE.md 畫面 13 第 4 點）。 */
export const MAX_OPEN = 10;
/** 「已開啟 N 篇」顯示多久（EXPERIENCE.md：按鈕原地顯示結果 2 秒）。 */
const FLASH_MS = 2000;

/** 900px 以上（動作列在上緣）或以下（固定在底部、原地列出連結）；寬度改變時重新渲染。只渲染看得到的那一個動作列。 */
function subscribeDesktop(onChange: () => void): () => void {
  const query = window.matchMedia(DESKTOP_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}
const isDesktop = () => window.matchMedia(DESKTOP_QUERY).matches;

interface LibraryPageProps {
  /** 網址上的「目前顯示幾篇」（按過「再載入」才有）。 */
  shown: number | undefined;
  onMore: (shown: number) => void;
  onSignedOut: () => void;
  /** 這一頁認定的成員（路由的 context）：清單的讀取帶它當預期成員（ADR-0055）。 */
  memberId: string;
  /** 清單的讀取回 409 member_changed（另一個分頁換了人；ADR-0055）。 */
  onMemberChanged: () => void | Promise<void>;
  /** 換人進行中（路由還沒換成新的人）：清單先不讀，是骨架（審查 fix2 N1）。 */
  paused?: boolean;
}

type Note = { kind: "skipped"; count: number } | { kind: "blocked" | "mobile"; links: { id: string; title: string; url: string }[] };

/**
 * 開新分頁並看有沒有被瀏覽器擋住：先開空白分頁（被擋時回 null），切斷和這一頁的關係（opener）之後再前往連結。
 * 直接帶 noopener 開的話一律回 null，分不出有沒有被擋。回 false 表示被擋住了。
 */
function openTab(url: string): boolean {
  const tab = window.open("", "_blank");
  if (tab === null) return false;
  tab.opener = null;
  tab.location.replace(url);
  return true;
}

export function LibraryPage({ shown, onMore, onSignedOut, memberId, onMemberChanged, paused = false }: LibraryPageProps) {
  const { t } = useTranslation("library");
  const { t: tc } = useTranslation("common");
  const queryClient = useQueryClient();
  const config = useQuery(clientConfigQuery);
  const names = useQuery(displayNamesQuery);
  const pageSize = config.data?.kind === "found" ? config.data.config.library_page_size : null;
  const limit = pageSize === null ? null : Math.max(shown ?? pageSize, pageSize);
  const list = useQuery({
    ...libraryListQuery(queryClient, limit ?? 0, memberId),
    enabled: limit !== null && !paused,
    placeholderData: (previous) => previous,
  });
  // 移出、復原帶「這份清單是用誰的身分讀的」（ADR-0055）。
  const change = useLibraryChange(list.data?.kind === "found" ? list.data.readAs : undefined);
  const desktop = useSyncExternalStore(subscribeDesktop, isDesktop, () => true);

  // 離開這一頁就丟掉清單的快取：再回來時重讀，移出的那幾篇不再出現（State Patterns「離開頁面後消失」）。
  useEffect(() => () => queryClient.removeQueries({ queryKey: ["library"] }), [queryClient]);

  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  // 移出了的（值是能不能復原：這一次真的移出了才能）。
  const [gone, setGone] = useState<ReadonlyMap<string, boolean>>(new Map());
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  // 送出中的那幾篇、目前移出了的那幾篇，同步記一份（審查 G7）：同一次畫面裡連按兩下時，第二下還看不到 busy／gone 的
  // 新狀態，靠這兩份擋掉第二次送出，也不讓篇數多扣或多加。
  const sending = useRef(new Set<string>());
  const goneNow = useRef(new Set<string>());
  // 篇數＝API 的 total 加上「讀到清單之後」移出（−1）與復原（+1）的篇數；更早的已經算在 total 裡。
  const [changes, setChanges] = useState<{ at: number; delta: number }[]>([]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [opened, setOpened] = useState<number | null>(null);
  const [focus, setFocus] = useState<{ id: string; target: "remove" | "restore" | "gone" } | null>(null);
  const flashTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(flashTimer.current), []);

  const signedOut = [config.data, names.data, list.data].some((data) => data?.kind === "signedOut");
  const notified = useRef(false);
  useEffect(() => {
    if (!signedOut || notified.current) return;
    notified.current = true;
    onSignedOut();
  }, [signedOut, onSignedOut]);
  // 讀取時後端說換了人（409，ADR-0055）：照「發現換人」處理；這段期間清單是骨架。每一份 409 的結果只處理一次
  // （同 SearchPage；審查 fix2 N1）。用這次結果的時間（dataUpdatedAt）認是哪一份，不用結果物件：結構共享在內容相同時
  // 沿用同一個物件，重讀又是 409 時會被當成處理過（審查 fix3 N1）。
  const memberChanged = list.data?.kind === "memberChanged" ? list.dataUpdatedAt : undefined;
  const followed = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (memberChanged === undefined || followed.current === memberChanged) return;
    followed.current = memberChanged;
    void onMemberChanged();
  }, [memberChanged, onMemberChanged]);

  // 直接打開（重新整理）網址時最多讀 MAX_SHOWN 篇（libraryListQuery）：網址的 shown 比讀到的多、後面還有的時候，
  // 把 shown 對齊成讀到的篇數。不對齊的話 shown 剛好是 MAX_SHOWN＋一頁時，「再載入」算出來的篇數就是現在的 shown，
  // 網址不變、什麼都不會發生（審查 G8）。對齊後的清單從讀到的那一份接著用，不重讀；對齊之後把原本那一份（只讀到
  // MAX_SHOWN 篇、卻記在較大的篇數底下）丟掉，之後再載入到那個篇數時才會從對齊後的那一份接著讀。
  const alignedFrom = useRef<number | null>(null);
  useEffect(() => {
    if (alignedFrom.current === null || limit === alignedFrom.current) return;
    queryClient.removeQueries({ queryKey: libraryListKey(alignedFrom.current, memberId), exact: true });
    alignedFrom.current = null;
  }, [limit, queryClient, memberId]);
  const capped =
    list.data?.kind === "found" &&
    !list.isPlaceholderData &&
    limit !== null &&
    list.data.list.items.length < limit &&
    (list.data.list.pending.length > 0 || list.data.list.next_cursor !== null)
      ? list.data.list.items.length
      : null;
  useEffect(() => {
    if (capped === null) return;
    alignedFrom.current = limit;
    onMore(capped);
  }, [capped, limit, onMore]);

  // 移出、復原之後，焦點移到同一列的另一個按鈕（設計稿：移出 → 復原、復原 → 移出）。
  useEffect(() => {
    if (focus === null) return;
    const selector = `[data-library-row="${CSS.escape(focus.id)}"] [data-library="${focus.target}"]`;
    for (const element of document.querySelectorAll<HTMLElement>(selector)) {
      if (element.offsetParent !== null) {
        element.focus();
        break;
      }
    }
  }, [focus]);

  if (signedOut) {
    return null;
  }

  const found = list.data?.kind === "found" ? list.data.list : null;
  const failed = [config, names, list].some((query) => query.isError && query.data === undefined);
  const namesData = names.data?.kind === "found" ? names.data.names : null;

  const clearNotes = () => {
    setNotes([]);
    setOpened(null);
  };
  const setOf = (set: ReadonlySet<string>, id: string, on: boolean) => {
    const next = new Set(set);
    if (on) next.add(id);
    else next.delete(id);
    return next;
  };

  const run = (item: LibraryItem, action: Extract<LibraryAction, "remove" | "restore">) => {
    const id = item.paper.id;
    if (sending.current.has(id)) return;
    sending.current.add(id);
    setBusy((current) => setOf(current, id, true));
    void change(id, action).then((outcome) => {
      sending.current.delete(id);
      setBusy((current) => setOf(current, id, false));
      if (outcome === "failed") return;
      // 篇數只在「移出了／還在」真的改變時增減。
      const wasGone = goneNow.current.has(id);
      if (action === "remove") goneNow.current.add(id);
      else goneNow.current.delete(id);
      const changed = wasGone !== (action === "remove");
      setGone((current) => {
        const next = new Map(current);
        if (action === "remove") next.set(id, outcome === "done");
        else next.delete(id);
        return next;
      });
      if (changed) setChanges((current) => [...current, { at: Date.now(), delta: action === "remove" ? -1 : 1 }]);
      if (action === "remove") setSelected((current) => setOf(current, id, false));
      clearNotes();
      // 不能復原的那一列沒有按鈕可以接焦點：焦點交給「已移出」那一段字。
      setFocus({ id, target: action === "restore" ? "remove" : outcome === "done" ? "restore" : "gone" });
    });
  };

  let body: ReactNode;
  // 899px 以下勾了論文時的底部選取列（放在內容外面，見下面的 return）。
  let dock: ReactNode = null;
  if (failed) {
    body = (
      <ErrorBar
        message={t("library.error.message")}
        action={tc("states.loadFailed.action")}
        onRetry={() => {
          for (const query of [config, names, list]) {
            if (query.isError) void query.refetch();
          }
        }}
      />
    );
  } else if (found === null || limit === null || namesData === null) {
    body = <LibrarySkeleton label={t("library.list.loading")} />;
  } else if (found.total === 0 && found.items.length === 0) {
    body = <EmptyLibrary />;
  } else {
    const items = found.items.slice(0, limit);
    const count = found.total + changes.filter((entry) => entry.at > list.dataUpdatedAt).reduce((sum, entry) => sum + entry.delta, 0);
    const goneShown = items.filter((item) => gone.has(item.paper.id)).length;
    const picked = items.filter((item) => selected.has(item.paper.id) && !gone.has(item.paper.id));
    const over = picked.length > MAX_OPEN;
    const loadingMore = list.isPlaceholderData && list.fetchStatus !== "idle";
    // 「已顯示 n／N」：和設計稿一樣，已移出還在畫面上的那幾列也算。
    const moreTotal = count + goneShown;
    const hasMore =
      items.length < moreTotal && (found.items.length > items.length || found.pending.length > 0 || found.next_cursor !== null);

    const openPicked = () => {
      const links = picked.flatMap((item) => {
        const url = externalUrl(item.paper.doi_url);
        return url === null ? [] : [{ id: item.paper.id, title: item.paper.title, url }];
      });
      const skipped = picked.length - links.length;
      const next: Note[] = [];
      window.clearTimeout(flashTimer.current);
      setOpened(null);
      if (!isDesktop()) {
        if (links.length > 0) next.push({ kind: "mobile", links });
      } else if (links.length > 0) {
        // 第一個被擋住的那一篇起，後面的都沒開出來（已經開了的不再列）。
        const firstBlocked = links.findIndex((link) => !openTab(link.url));
        if (firstBlocked >= 0) {
          next.push({ kind: "blocked", links: links.slice(firstBlocked) });
        } else {
          setOpened(links.length);
          flashTimer.current = window.setTimeout(() => setOpened(null), FLASH_MS);
        }
      }
      if (skipped > 0) next.push({ kind: "skipped", count: skipped });
      setNotes(next);
    };
    const unselect = () => {
      setSelected(new Set());
      clearNotes();
    };
    const actions = { count: picked.length, over, opened, onOpen: openPicked, onCancel: unselect };
    // 批次開啟的結果與說明（設計稿 .notes）：桌機在選取列下面、一起黏在上緣；手機在底部選取列的最上面（決定第 90、91 項）。
    const notesBlock = <Notes notes={notes} mobile={!desktop} />;
    if (picked.length > 0 && !desktop) dock = <Dock {...actions} notes={notesBlock} />;

    body = (
      <>
        {/* 寫篇數的那一行／選取列（＋結果與說明）：900px 以上黏在視窗上緣（設計稿 .slot），899px 以下不黏。 */}
        <div className="mt-2 bg-background split:sticky split:top-[0px] split:z-[4] split:mt-[20px] split:py-3">
          <div className={`flex min-h-[40px] items-center ${picked.length > 0 ? "split:hidden" : ""}`}>
            <h2 data-library-count className="font-mono text-s13 tracking-006 text-text-secondary">
              {/* 數字自己一段（設計稿 .cn），樣式和整句一樣。 */}
              {splitTemplate(t("library.list.count")).map((part, index) =>
                part.slot === null ? part.text : <span key={index}>{formatCount(count)}</span>,
              )}
            </h2>
          </div>
          {picked.length > 0 && desktop ? (
            <>
              <ActionBar {...actions} />
              {notesBlock}
            </>
          ) : null}
        </div>
        <ul data-library-rows className="border-y border-hairline">
          {items.map((item, index) => (
            <LibraryRow
              key={item.paper.id}
              item={item}
              first={index === 0}
              names={namesData}
              selected={selected.has(item.paper.id)}
              gone={gone.has(item.paper.id)}
              restorable={gone.get(item.paper.id) === true}
              busy={busy.has(item.paper.id)}
              onSelect={(on) => {
                setSelected((current) => setOf(current, item.paper.id, on));
                clearNotes();
              }}
              onRemove={() => run(item, "remove")}
              onRestore={() => run(item, "restore")}
            />
          ))}
        </ul>
        {hasMore && pageSize !== null ? (
          <More
            shown={items.length}
            total={moreTotal}
            pageSize={pageSize}
            next={Math.min(pageSize, moreTotal - items.length)}
            loading={loadingMore}
            onMore={() => onMore(items.length + pageSize)}
          />
        ) : null}
      </>
    );
  }

  // 這一頁撐滿 main 的高度（外框 main 是直排的 flex）：底部選取列前面有一段會伸長的空白（最少 32px，設計稿 margin-top 32），
  // 清單很短時選取列也在視窗最下面；有選取列時往下吃掉 main 的下內距（899px 以下 64px、900px 以上 96px），選取列貼齊
  // 全站頁尾的上緣（設計稿 library.html 390：.pp 至少一個視窗高、main flex:1、.dock 接在 main 後面；審查 R9）。
  // 選取列是 sticky，只在這一頁的範圍內移動：捲到最底時停在頁尾上面，不會蓋住頁尾（Story 3.15）。
  return (
    <div
      data-page="library"
      className={`flex flex-1 flex-col text-s14 leading-normal tracking-0 text-text-primary split:max-w-reading-width ${dock === null ? "" : "-mb-[64px] split:-mb-24"}`}
    >
      <div>
        <h1 className="font-title-zh text-page-title-zh text-text-primary">{t("library.page.title")}</h1>
        {body}
        <NewTabNote />
      </div>
      {dock === null ? null : (
        <>
          <div aria-hidden="true" className="min-h-8 flex-1" />
          {dock}
        </>
      )}
    </div>
  );
}

interface ActionProps {
  count: number;
  over: boolean;
  /** 剛開了幾篇（「已開啟 N 篇」顯示中）；沒有是 null。 */
  opened: number | null;
  onOpen: () => void;
  onCancel: () => void;
}

/**
 * 「開啟全文」（設計稿 button.go：40px 高、暖白底、14px／500／0.06em、字後箭頭；停用時透明、髮線框、第三層字色）。
 * 按了之後原地顯示「已開啟 N 篇」2 秒。看得到的 40px，按鈕本身 44px（ADR-0037），負外距抵掉。
 */
function OpenButton({ over, opened, onOpen, className }: ActionProps & { className: string }) {
  const { t } = useTranslation("library");
  return (
    <button
      type="button"
      data-library="open"
      disabled={over}
      onClick={onOpen}
      className="group -my-[2px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px] disabled:cursor-default"
    >
      <span
        className={`inline-flex h-[40px] w-full items-center justify-center rounded-full border border-text-primary bg-text-primary text-button text-s14 tracking-006 whitespace-nowrap text-on-button transition-transform duration-200 group-enabled:group-hover:-translate-y-px group-disabled:border-hairline group-disabled:bg-transparent group-disabled:text-text-tertiary ${className}`}
      >
        {opened === null ? (
          <>
            {t("library.actions.open")}
            <OpenArrow className="ml-[6px]" />
          </>
        ) : (
          t("library.actions.opened", { count: opened })
        )}
      </span>
    </button>
  );
}

function OpenArrow({ className }: { className: string }) {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true" className={`shrink-0 ${className}`}>
      <path d="M2 8L8 2M3.5 2H8v4.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 「已選 N 篇」（設計稿 .selc：DM Mono 13px、0.06em、主要字色；數字自己一段 .gn）。 */
function SelectedCount({ count }: { count: number }) {
  const { t } = useTranslation("library");
  return (
    <span data-library-selected className="font-mono text-s13 tracking-006 whitespace-nowrap text-text-primary">
      {splitTemplate(t("library.actions.selected")).map((part, index) =>
        part.slot === null ? part.text : <span key={index}>{count}</span>,
      )}
    </span>
  );
}

function MaxNote() {
  const { t } = useTranslation("library");
  return <span className="font-mono text-s12 tracking-006 text-text-secondary">{t("library.actions.max")}</span>;
}

/**
 * 「取消」（設計稿 button.txt.unsel：13px、第二層字色）；看得到的 28px（手機 40px）。可點範圍高 44px、左右各外擴 9px
 * （設計稿 .txt.hit），負外距抵掉。
 */
function CancelButton({ onCancel, className }: { onCancel: () => void; className: string }) {
  const { t } = useTranslation("library");
  return (
    <button
      type="button"
      data-library="cancel"
      onClick={onCancel}
      className={`-mx-[9px] inline-flex h-tap-target cursor-pointer items-center bg-transparent px-[9px] py-[0px] text-s13 tracking-004 whitespace-nowrap text-text-secondary transition-[color] duration-200 hover:text-text-primary ${className}`}
    >
      {t("library.actions.cancel")}
    </button>
  );
}

/** 900px 以上的選取列（設計稿 .abar）：原地換掉寫篇數的那一行；「已選 N 篇」→「開啟全文」→（最多 10 篇）→「取消」。 */
function ActionBar(props: ActionProps) {
  return (
    <div data-library-actions className="hidden min-h-[40px] flex-wrap items-center gap-x-[20px] gap-y-2 split:flex">
      <SelectedCount count={props.count} />
      <OpenButton {...props} className="px-[22px]" />
      {props.over ? <MaxNote /> : null}
      <CancelButton onCancel={props.onCancel} className="-my-2" />
    </div>
  );
}

/**
 * 899px 以下的選取列（設計稿 .dock）：黏在視窗底部（清單很短時也在最下面，見 LibraryPage 的 return）。由上到下：結果與說明（有才佔位）、「最多 10 篇」（各佔一整行）、
 * 左邊「已選 N 篇」、右邊「取消」＋「開啟全文」。
 */
function Dock({ notes, ...props }: ActionProps & { notes: ReactNode }) {
  return (
    <div
      data-library-dock
      className="sticky bottom-[0px] z-[4] -mx-gutter-mobile flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-hairline bg-background px-gutter-mobile pt-3 pb-[14px] tablet:-mx-gutter-tablet tablet:px-gutter-tablet split:hidden"
    >
      {notes}
      {props.over ? (
        <span className="basis-full">
          <MaxNote />
        </span>
      ) : null}
      <SelectedCount count={props.count} />
      <span className="inline-flex items-center gap-[20px]">
        <CancelButton onCancel={props.onCancel} className="-my-[2px]" />
        <OpenButton {...props} className="px-[20px]" />
      </span>
    </div>
  );
}

/**
 * 批次開啟的結果與說明（設計稿 .notes）：讀得到（role=status）。桌機在選取列下面（每段上 10、下 4；連結清單最高 min(40vh,300px)，
 * 超過在裡面捲）；手機在底部選取列的最上面、佔一整行（每段下 12，相鄰的那一段往上 4；連結清單最高 38vh；沒有內容時不佔位，但留在無障礙樹裡）。
 */
function Notes({ notes, mobile }: { notes: Note[]; mobile: boolean }) {
  return (
    // 手機：沒有內容時用只給螢幕閱讀器的寫法（絕對定位、1px），不佔底部選取列的一行，但一直在無障礙樹裡；
    // display:none 的即時播報區在內容插進來的同時才出現，螢幕閱讀器常常不念（審查 R11）。
    <div role="status" data-library-notes className={mobile ? "basis-full empty:sr-only" : ""}>
      {notes.map((note, index) => (
        <NoteBlock key={`${note.kind}-${index}`} note={note} mobile={mobile} first={index === 0} />
      ))}
    </div>
  );
}

/** 一段說明（設計稿 .anote）：圖示＋14px 的字；有連結時下面接一串連結（.flist）。 */
function NoteBlock({ note, mobile, first }: { note: Note; mobile: boolean; first: boolean }) {
  const { t } = useTranslation("library");
  const text =
    note.kind === "skipped"
      ? t("library.notes.skipped", { count: note.count })
      : t(note.kind === "blocked" ? "library.notes.blocked" : "library.notes.mobile");
  return (
    <div data-library-note={note.kind} className={mobile ? `pb-3 ${first ? "" : "-mt-1"}` : "pt-[10px] pb-1"}>
      <div className="flex items-start gap-[10px] text-s14 leading-170 text-text-reading">
        <span className="flex text-text-secondary">
          <NoticeIcon className="mt-[6px]" />
        </span>
        <span>{text}</span>
      </div>
      {note.kind === "skipped" ? null : (
        <div
          className={`mt-[10px] overflow-auto border-t border-hairline ${mobile ? "max-h-[38vh] overscroll-contain" : "max-h-[min(40vh,300px)]"}`}
        >
          {note.links.map((link, index) => (
            <a
              key={link.id}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              aria-describedby={NEW_TAB_NOTE_ID}
              className={`flex items-baseline gap-[6px] py-3 font-display text-s18 leading-135 text-text-primary no-underline transition-[color] duration-200 hover:text-accent-gold ${
                index < note.links.length - 1 ? "border-b border-hairline" : ""
              }`}
            >
              <span className="min-w-[0px]">{link.title}</span>
              <OpenArrow className="ml-[2px] text-accent-gold" />
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

interface RowProps {
  item: LibraryItem;
  first: boolean;
  names: Parameters<typeof Meta>[0]["names"];
  selected: boolean;
  gone: boolean;
  /** 移出了而且能復原（這一次真的移出了）。 */
  restorable: boolean;
  busy: boolean;
  onSelect: (on: boolean) => void;
  onRemove: () => void;
  onRestore: () => void;
}

/**
 * 一篇論文（設計稿第二版 .lrow）：勾選框、標題（連到論文頁）、期刊 · 年份；右側「移出」＋「開啟全文」（沒有 DOI 的只有「移出」）。
 * 900px 以上右側一欄寬 120（.lact：「移出」靠左、「開啟全文」靠右）；899px 以下兩個在書目下面一行（間距 24）。
 * 移出後是「已移出」＋標題＋「復原」。
 */
function LibraryRow({ item, first, names, selected, gone, restorable, busy, onSelect, onRemove, onRestore }: RowProps) {
  const { t } = useTranslation("library");
  const paper = item.paper;
  const doiUrl = externalUrl(paper.doi_url);
  const remove = (className: string) => (
    <RemoveText title={paper.title} label={t("library.row.remove")} busy={busy} onRemove={onRemove} className={className} />
  );
  return (
    // 沒移出時 li 自己就是一排（勾選框、文字、右側動作；設計稿 .lbody），不多包一層：標題的文字離 li 的上框線不超過
    // 四層，比對工具才量得到每列上方的髮線（設計稿也是三層）。
    <li
      data-library-row={paper.id}
      className={`group py-[20px] split:py-[22px] ${first ? "" : "border-t border-hairline"} ${gone ? "" : "flex items-start gap-2 split:gap-3"}`}
    >
      {gone ? (
        <div data-library-gone className="flex min-h-[28px] items-center gap-4">
          <span data-library="gone" tabIndex={-1} className="shrink-0 font-mono text-s13 tracking-006 text-text-secondary">
            {t("library.row.removed")}
          </span>
          <span className="min-w-[0px] flex-1 truncate font-display text-s17 text-text-tertiary split:text-s18">{paper.title}</span>
          {restorable ? (
            <button
              type="button"
              data-library="restore"
              disabled={busy}
              aria-busy={busy || undefined}
              onClick={onRestore}
              className="-my-2 inline-flex h-tap-target min-w-tap-target cursor-pointer items-center justify-end bg-transparent p-[0px] text-s13 tracking-004 whitespace-nowrap text-accent-gold decoration-1 underline-offset-4 hover:underline disabled:cursor-default"
            >
              {t("library.row.restore")}
            </button>
          ) : null}
        </div>
      ) : (
        <>
          {/* 看得到的勾選框 16px、可點範圍 28px（設計稿 .ck）；label 與輸入框補到 44×44（ADR-0037），負外距抵掉。
              框線平常是第三層字色，滑到那一列變第二層、滑到勾選框變主要字色；勾了填主要字色（設計稿第二版「勾選框退到次要」）。
              焦點外框照全站規則（外擴 4px，畫在 44×44 外面；設計稿是勾選框外 3px，ADR-0037 兩條規則同時要守，列進決定紀錄）。 */}
          <label className="relative -my-2 -mr-2 -ml-[14px] inline-flex size-tap-target shrink-0 cursor-pointer items-center justify-center">
            <input
              type="checkbox"
              checked={selected}
              onChange={(event) => onSelect(event.target.checked)}
              aria-label={t("library.list.select", { title: paper.title })}
              className="peer absolute inset-[0px] m-[0px] cursor-pointer appearance-none bg-transparent"
            />
            <span
              aria-hidden="true"
              className={`inline-flex size-4 items-center justify-center border transition-[background-color,border-color] duration-150 ${
                selected
                  ? "border-text-primary bg-text-primary"
                  : "border-text-tertiary group-hover:border-text-secondary peer-hover:border-text-primary"
              }`}
            >
              {selected ? (
                <svg width="10" height="8" viewBox="0 0 10 8" fill="none" aria-hidden="true" className="text-on-button">
                  <path d="M1 4l2.8 2.8L9 1.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : null}
            </span>
          </label>
          <div className="min-w-[0px] flex-1">
            <RowTitle id={paper.id} title={paper.title} />
            <Meta row={paper} names={names} noAbstract={false} />
            <div className="mt-2 flex items-center gap-6 split:hidden">
              {remove("-ml-[9px]")}
              {doiUrl === null ? null : <OpenFullText href={doiUrl} size="compact" />}
            </div>
          </div>
          <div className="hidden min-h-[28px] w-[120px] shrink-0 items-center justify-between split:flex">
            {remove("-ml-[9px]")}
            {doiUrl === null ? null : <OpenFullText href={doiUrl} size="compact" />}
          </div>
        </>
      )}
    </li>
  );
}

/** 空的（AR-67 過渡做法，Kevin 2026-11-14 決定）：訊息條＋一行「在論文頁按『加入我的文獻庫』」。 */
function EmptyLibrary() {
  const { t } = useTranslation("library");
  return (
    <div data-state="empty">
      <div role="status" className="mt-[36px] flex items-center gap-[14px] split:mt-12">
        <span className="flex text-text-secondary">
          <NoticeIcon />
        </span>
        <p className="text-s17 leading-170 text-text-primary">{t("library.empty.message")}</p>
      </div>
      <div className="mt-6 max-w-[520px] border-b border-hairline">
        <p className="border-t border-hairline py-3 text-s16 leading-160 text-text-reading">{t("library.empty.fromPaper")}</p>
      </div>
    </div>
  );
}

// 載入中的骨架（設計稿「載入中」）：寫篇數那一行＋5 列（勾選框、標題、期刊年份）。設計稿每列第三條（AI 整理那一行）不畫（D1）。
const SKELETON_WIDTHS = [
  [231, 420],
  [286, 520],
  [209, 380],
  [264, 480],
  [242, 440],
] as const;

function Bar({ className, width }: { className: string; width: readonly [number, number] }) {
  const style = { "--bar-mobile": `${width[0]}px`, "--bar-desktop": `${width[1]}px` } as CSSProperties;
  return <div style={style} className={`w-(--bar-mobile) max-w-full bg-hairline split:w-(--bar-desktop) ${className}`} />;
}

function LibrarySkeleton({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} data-state="loading" className="mt-8 split:mt-[44px]">
      <Bar className="h-3" width={[280, 420]} />
      <div aria-hidden="true" className="mt-[26px] border-y border-hairline">
        {SKELETON_WIDTHS.map((width, index) => (
          <div key={index} className={`flex items-start gap-4 py-[26px] ${index > 0 ? "border-t border-hairline" : ""}`}>
            <div className="size-4 shrink-0 border border-hairline" />
            <div className="min-w-[0px] flex-1">
              <Bar className="h-4" width={width} />
              <div className="mt-4 h-[10px] w-[96px] max-w-full bg-hairline" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
