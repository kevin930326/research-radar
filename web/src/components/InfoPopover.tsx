import { Popover } from "@base-ui/react/popover";
import { useId, type ReactNode } from "react";

// 說明框（DESIGN.md Components「說明框」、EXPERIENCE.md Component Patterns「說明框」）：
// 滑鼠滑過、點一下（手機）、鍵盤 Enter／空白鍵都會出現；移開、按 Esc、點別處會消失。
// 行為用 Base UI Popover（ADR-0010 唯一的元件庫），外觀只用代幣：
// 半透明深底、1px 框、直角、caption 字級、內距 7px 11px、最寬 320px、淡入 0.2 秒。
// 觸發的按鈕可點範圍至少 44×44；看得到的樣子（例如 22px 高的狀態標記）放在 trigger 裡。
// 說明框（role=dialog）的名稱就是觸發按鈕上的字（例如「AI 整理」「追蹤範圍外」，EXPERIENCE.md 既有的說法），
// 用 aria-labelledby 指過去，不另寫文字。浮在頁面上一層（DESIGN.md「只有兩層」），所以定位層加 z-10。

const TRIGGER_CLASS =
  "inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center justify-center text-left";

const POPUP_CLASS =
  "max-w-[320px] rounded-none border border-border bg-popover-surface px-[11px] py-[7px] font-body text-caption text-text-primary transition-opacity duration-200 data-[starting-style]:opacity-0 data-[ending-style]:opacity-0";

/** 滑過多久才出現（毫秒）。 */
const HOVER_DELAY_MS = 150;

interface InfoPopoverProps {
  /** 觸發的按鈕裡看得到的東西。 */
  trigger: ReactNode;
  /** 說明框的內容。 */
  children: ReactNode;
  /** 觸發按鈕額外的類別（例如對齊方式）。 */
  triggerClassName?: string;
}

export function InfoPopover({ trigger, children, triggerClassName }: InfoPopoverProps) {
  const triggerId = useId();
  return (
    <Popover.Root>
      <Popover.Trigger
        id={triggerId}
        data-info-popover-trigger
        openOnHover
        delay={HOVER_DELAY_MS}
        className={triggerClassName === undefined ? TRIGGER_CLASS : `${TRIGGER_CLASS} ${triggerClassName}`}
      >
        {trigger}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner sideOffset={4} align="start" className="z-10">
          <Popover.Popup data-info-popover aria-labelledby={triggerId} className={POPUP_CLASS}>
            {children}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
