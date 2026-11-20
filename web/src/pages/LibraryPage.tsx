import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { NEW_TAB_NOTE_ID, NewTabNote } from "../components/ExternalArrow";
import { PaperTitle } from "../components/PaperText";
import { Meta } from "../components/search/PaperRows";
import { NoticeIcon } from "../components/search/parts";
import { DESKTOP_QUERY } from "../hooks/use-close-across-breakpoint";
import type { LibraryItem } from "../datasource/types";
import { useLibraryChange, type LibraryAction } from "../routes/library-change";
import { clientConfigQuery, displayNamesQuery, libraryListQuery } from "../routes/paper-queries";
import { externalUrl, formatCount, splitTemplate } from "./paper-format";
import { ErrorBar, More } from "./SearchPage";

// 我的文獻庫（Story 3.17；EXPERIENCE.md 畫面 13、State Patterns「13. 我的文獻庫」；設計稿 docs/ux/reference/screens/library.html）。
// 照 AR-67 的基本版：標題、篇數、論文列（勾選框、標題、期刊 · 年份、「移出」）、勾選後的動作列、再載入。
// 不做（AR-67，Epic 6）：右欄（上傳或匯入、儲存空間）、「怎麼加入的」篩選、「有我的 PDF」「追蹤範圍外」兩個開關、
// 「有 N 篇等你確認」、「● 有我的 PDF」、不能直接移出的原因；每列第三行「AI 整理」不做（D1，Kevin 2026-11-20 決定）。
// - 一次列 client-config 的 library_page_size 篇（50）；「再載入 N 篇」「已顯示 n／N」，剩下不到一頁時寫實際的篇數，
//   全部列出來以後不顯示。目前顯示幾篇寫進網址（shown，AD-21）。
// - 勾選後：900px 以上，寫篇數的那一行原地換成動作列（黏在視窗上緣）；899px 以下動作列固定在畫面底部。
//   「開啟選取的全文（N 篇）」：一次最多 10 篇（超過時停用，旁邊「一次最多開 10 篇」）；每篇開一個新分頁（DOI 連結），
//   沒有 DOI 的跳過（「N 篇沒有全文連結，已跳過」）；開了之後按鈕原地顯示「已開啟 N 篇」2 秒；
//   瀏覽器擋住新分頁時不顯示這一句，改在動作列下方列出連結。手機一次開不了多個分頁：原地列出連結。
//   瀏覽器擋住時列出的是還沒開出來的那幾篇（已經開了的不重複列）。
//   連結只開後端給的 DOI 連結（externalUrl 只收 http／https），不組學校代理或付費牆的網址。
// - 「移出」：那一列原地變成「已移出」＋「復原」，不跳確認；離開這一頁後消失（離開時丟掉文獻庫清單的快取）。
//   後端回 404（已經不在，例如另一個分頁移出了）時只顯示「已移出」、不給「復原」：這一次沒有真的移出，
//   復原會拿到別次移出的封存（ADR-0049 復原取最新一筆；審查 F3）。
// - 空的：「你的文獻庫還沒有論文」＋一行「在論文頁按『加入我的文獻庫』」（AR-67 過渡做法，Kevin 2026-11-14 決定；
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

export function LibraryPage({ shown, onMore, onSignedOut }: LibraryPageProps) {
  const { t } = useTranslation("library");
  const { t: tc } = useTranslation("common");
  const queryClient = useQueryClient();
  const config = useQuery(clientConfigQuery);
  const names = useQuery(displayNamesQuery);
  const pageSize = config.data?.kind === "found" ? config.data.config.library_page_size : null;
  const limit = pageSize === null ? null : Math.max(shown ?? pageSize, pageSize);
  const list = useQuery({
    ...libraryListQuery(queryClient, limit ?? 0),
    enabled: limit !== null,
    placeholderData: (previous) => previous,
  });
  const change = useLibraryChange();
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

  // 直接打開（重新整理）網址時最多讀 MAX_SHOWN 篇（libraryListQuery）：網址的 shown 比讀到的多、後面還有的時候，
  // 把 shown 對齊成讀到的篇數。不對齊的話 shown 剛好是 MAX_SHOWN＋一頁時，「再載入」算出來的篇數就是現在的 shown，
  // 網址不變、什麼都不會發生（審查 G8）。對齊後的清單從讀到的那一份接著用，不重讀；對齊之後把原本那一份（只讀到
  // MAX_SHOWN 篇、卻記在較大的篇數底下）丟掉，之後再載入到那個篇數時才會從對齊後的那一份接著讀。
  const alignedFrom = useRef<number | null>(null);
  useEffect(() => {
    if (alignedFrom.current === null || limit === alignedFrom.current) return;
    queryClient.removeQueries({ queryKey: ["library", alignedFrom.current], exact: true });
    alignedFrom.current = null;
  }, [limit, queryClient]);
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

    body = (
      <>
        {/* 寫篇數的那一行／動作列：900px 以上黏在視窗上緣（設計稿 .slot），899px 以下不黏。 */}
        <div className="mt-2 bg-background split:sticky split:top-[0px] split:z-[4] split:mt-[20px] split:py-3">
          <div className={`flex min-h-[40px] items-center ${picked.length > 0 ? "split:hidden" : ""}`}>
            <h2 data-library-count className="font-mono text-s13 tracking-006 text-text-secondary">
              {/* 數字自己一段（設計稿 .cn），樣式和整句一樣。 */}
              {splitTemplate(t("library.list.count")).map((part, index) =>
                part.slot === null ? part.text : <span key={index}>{formatCount(count)}</span>,
              )}
            </h2>
          </div>
          {picked.length > 0 && desktop ? <ActionBar {...actions} /> : null}
        </div>
        <div role="status" data-library-notes>
          {notes.map((note, index) => (
            <NoteBlock key={`${note.kind}-${index}`} note={note} />
          ))}
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
        {picked.length > 0 && !desktop ? <Dock {...actions} /> : null}
      </>
    );
  }

  return (
    <div data-page="library" className="text-s14 leading-normal tracking-0 text-text-primary split:max-w-reading-width">
      <h1 className="font-title-zh text-page-title-zh text-text-primary">{t("library.page.title")}</h1>
      {body}
      <NewTabNote />
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
 * 「開啟選取的全文（N 篇）」（設計稿 .go：40px 高、暖白底；停用時透明、髮線框、第三層字色）。
 * 看得到的 40px，按鈕本身 44px（ADR-0037），負外距抵掉。
 */
function OpenButton({ count, over, opened, onOpen, arrow, className }: ActionProps & { arrow: boolean; className: string }) {
  const { t } = useTranslation("library");
  return (
    <button
      type="button"
      data-library="open"
      disabled={over}
      onClick={onOpen}
      className={`group -my-[2px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px] disabled:cursor-default ${className}`}
    >
      <span
        className={`inline-flex h-[40px] w-full items-center justify-center rounded-full border border-text-primary bg-text-primary text-button text-s14 tracking-006 whitespace-nowrap text-on-button transition-transform duration-200 group-enabled:group-hover:-translate-y-px group-disabled:border-hairline group-disabled:bg-transparent group-disabled:text-text-tertiary ${
          arrow ? "px-[22px]" : "px-[18px]"
        }`}
      >
        {opened === null ? (
          <>
            {/* 數字自己一段（設計稿 .gn）。 */}
            {splitTemplate(t("library.actions.open")).map((part, index) =>
              part.slot === null ? part.text : <span key={index}>{count}</span>,
            )}
            {arrow ? <OpenArrow className="ml-[6px]" /> : null}
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

function MaxNote() {
  const { t } = useTranslation("library");
  return <span className="font-mono text-s12 tracking-006 text-text-secondary">{t("library.actions.max")}</span>;
}

/** 「取消選取」（設計稿 .txt：13px、第二層字色）；看得到的 28px（手機 40px），按鈕本身 44px（ADR-0037）。 */
function CancelButton({ onCancel, className }: { onCancel: () => void; className: string }) {
  const { t } = useTranslation("library");
  return (
    <button
      type="button"
      data-library="cancel"
      onClick={onCancel}
      className={`inline-flex h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s13 tracking-004 whitespace-nowrap text-text-secondary transition-[color] duration-200 hover:text-text-primary ${className}`}
    >
      {t("library.actions.cancel")}
    </button>
  );
}

/** 900px 以上的動作列（設計稿 .abar）：原地換掉寫篇數的那一行。 */
function ActionBar(props: ActionProps) {
  return (
    <div data-library-actions className="hidden min-h-[40px] flex-wrap items-center gap-x-[20px] gap-y-2 split:flex">
      <OpenButton {...props} arrow className="" />
      {props.over ? <MaxNote /> : null}
      <CancelButton onCancel={props.onCancel} className="-my-[2px]" />
    </div>
  );
}

/** 899px 以下的動作列（設計稿 .dock）：固定在畫面底部；「一次最多開 10 篇」在最上面一整行。 */
function Dock(props: ActionProps) {
  return (
    <div
      data-library-dock
      className="sticky bottom-[0px] z-[4] -mx-gutter-mobile mt-8 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t border-hairline bg-background px-gutter-mobile pt-3 pb-[14px] tablet:-mx-gutter-tablet tablet:px-gutter-tablet split:hidden"
    >
      {props.over ? (
        <span className="basis-full">
          <MaxNote />
        </span>
      ) : null}
      <OpenButton {...props} arrow={false} className="" />
      <CancelButton onCancel={props.onCancel} className="-my-[2px]" />
    </div>
  );
}

/** 動作列下方的一行（設計稿 .anote）：圖示＋14px 的字；有連結時下面接一串連結（.flist）。 */
function NoteBlock({ note }: { note: Note }) {
  const { t } = useTranslation("library");
  const text =
    note.kind === "skipped"
      ? t("library.notes.skipped", { count: note.count })
      : t(note.kind === "blocked" ? "library.notes.blocked" : "library.notes.mobile");
  return (
    <div data-library-note={note.kind} className="pt-1 pb-[20px]">
      <div className="flex items-start gap-[10px] text-s14 leading-170 text-text-reading">
        <span className="flex text-text-secondary">
          <NoticeIcon className="mt-[6px]" />
        </span>
        <span>{text}</span>
      </div>
      {note.kind === "skipped" ? null : (
        <div className="mt-[10px] border-t border-hairline">
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

/** 「移出」（設計稿 .txt.rm：13px、第三層字色，滑到或聚焦在那一列時變閱讀字色）；看得到的 28px，按鈕本身 44px。 */
function RemoveButton({ busy, onRemove, className }: { busy: boolean; onRemove: () => void; className: string }) {
  const { t } = useTranslation("library");
  return (
    <button
      type="button"
      data-library="remove"
      disabled={busy}
      aria-busy={busy || undefined}
      onClick={onRemove}
      className={`-my-2 inline-flex h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s13 tracking-004 whitespace-nowrap text-text-tertiary transition-[color] duration-200 group-focus-within:text-text-reading group-hover:text-text-reading hover:text-text-primary disabled:cursor-default ${className}`}
    >
      {t("library.row.remove")}
    </button>
  );
}

/** 一篇論文（設計稿 .lrow）：勾選框、標題（連到論文頁）、期刊 · 年份、「移出」；移出後是「已移出」＋標題＋「復原」。 */
function LibraryRow({ item, first, names, selected, gone, restorable, busy, onSelect, onRemove, onRestore }: RowProps) {
  const { t } = useTranslation("library");
  const paper = item.paper;
  return (
    <li
      data-library-row={paper.id}
      className={`group py-[20px] split:py-[22px] ${first ? "" : "border-t border-hairline"}`}
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
        <div className="flex items-start gap-2 split:gap-3">
          {/* 看得到的勾選框 16px、可點範圍 28px（設計稿 .ck）；label 與輸入框補到 44×44（ADR-0037），負外距抵掉。
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
              className={`inline-flex size-4 items-center justify-center border transition-[background-color,border-color] duration-150 peer-hover:border-text-primary ${
                selected ? "border-text-primary bg-text-primary" : "border-text-secondary"
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
            <PaperTitle as="h3" size="row" wrap="pretty" className="max-split:text-s20">
              {/* 標題一行 27px：上下各補 9px 透明的可點範圍（ADR-0037），負外距抵掉（和找論文的論文列一樣）。 */}
              <Link
                to="/papers/$paperId"
                params={{ paperId: paper.id }}
                className="-my-[9px] block py-[9px] text-text-primary no-underline decoration-1 underline-offset-[5px] hover:underline"
              >
                <span className="block">{paper.title}</span>
              </Link>
            </PaperTitle>
            <Meta row={paper} names={names} noAbstract={false} />
            <div className="mt-2 split:hidden">
              <RemoveButton busy={busy} onRemove={onRemove} className="justify-start" />
            </div>
          </div>
          <div className="hidden w-[56px] shrink-0 justify-end split:flex">
            <RemoveButton busy={busy} onRemove={onRemove} className="justify-end" />
          </div>
        </div>
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
