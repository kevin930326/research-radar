import type { ReactNode } from "react";

// 訊息條（DESIGN.md Components「訊息條」）：沒有底色與外框；左邊一個空心圖示，右邊一句話，
// 需要時再加一個可以做的動作。出現在內容原本的位置，不用浮動通知。

type Tone = "info" | "error";

function InfoIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true" className="text-text-secondary">
      <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1" />
      <path d="M8 7v4.5M8 4.5v1" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

function ErrorIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true" className="text-danger">
      <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1" />
      <path d="M8 4v5M8 10.5v1.5" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

interface MessageBarProps {
  tone: Tone;
  message: string;
  children?: ReactNode;
  role?: "status" | "alert";
}

export function MessageBar({ tone, message, children, role = "status" }: MessageBarProps) {
  return (
    <div role={role} className="flex items-start gap-3 py-3">
      <span className="flex h-tap-target shrink-0 items-center">{tone === "error" ? <ErrorIcon /> : <InfoIcon />}</span>
      <div className="flex flex-wrap items-center gap-x-6">
        <p className="flex min-h-tap-target items-center text-body text-text-primary">{message}</p>
        {children}
      </div>
    </div>
  );
}
