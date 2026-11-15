// 兩個 vite preview 與一個開發伺服器的埠；playwright.config.ts 與測試共用。
export const MEMBER_PORT = 4173;
export const DEMO_PORT = 4174;
/** 開發伺服器（成員版）：只給元件示範頁（/dev/components，只在開發環境）用。 */
export const DEV_PORT = 4175;

export const MEMBER_URL = `http://127.0.0.1:${MEMBER_PORT}`;
export const DEMO_URL = `http://127.0.0.1:${DEMO_PORT}`;
export const DEV_URL = `http://127.0.0.1:${DEV_PORT}`;
