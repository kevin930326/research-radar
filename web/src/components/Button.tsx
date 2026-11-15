import type { ButtonHTMLAttributes, ReactNode } from "react";

// 主要按鈕與次要按鈕（DESIGN.md Components、EXPERIENCE.md Component Patterns）：
// - 主要：暖白底、深色字、沒有框，高 50、全圓角、左右內距 32。滑過往上移 1px、箭頭往右移 4px（0.25 秒）。
//   一個畫面最多一個（由使用的畫面負責）。
// - 次要：透明底、1px 框、暖白字，尺寸同主要。滑過框變成暖白。
// - 停用：不透明度 40%。按下後要等超過 0.3 秒時，畫面把按鈕停用並把字換成進行中的說法（「登入中…」）。
// 過場只給位移與框色：焦點外框（outline）要立刻出現，不能跟著淡入。減少動態時，全站規則（styles.css）把過場關掉。

type Variant = "primary" | "secondary";

const BASE_CLASS =
  "group inline-flex h-button-height cursor-pointer items-center justify-center gap-2 rounded-full px-8 font-body text-button duration-250 disabled:cursor-not-allowed disabled:opacity-40";

const VARIANT_CLASS: Record<Variant, string> = {
  primary: "bg-text-primary text-on-button transition-transform enabled:hover:-translate-y-px",
  secondary:
    "border border-border bg-transparent text-text-primary transition-[border-color] enabled:hover:border-text-primary",
};

const ARROW = "→";

interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className" | "children"> {
  variant: Variant;
  children: ReactNode;
  /** 字後面加一個箭頭（主要按鈕滑過時往右移 4px）。 */
  arrow?: boolean;
}

export function Button({ variant, children, arrow = false, type = "button", ...props }: ButtonProps) {
  return (
    <button {...props} type={type} data-variant={variant} className={`${BASE_CLASS} ${VARIANT_CLASS[variant]}`}>
      <span>{children}</span>
      {arrow ? (
        <span aria-hidden="true" className="transition-transform duration-250 group-enabled:group-hover:translate-x-1">
          {ARROW}
        </span>
      ) : null}
    </button>
  );
}
