// 兩個 vite preview 的埠；playwright.config.ts 與測試共用。
export const MEMBER_PORT = 4173;
export const DEMO_PORT = 4174;

export const MEMBER_URL = `http://127.0.0.1:${MEMBER_PORT}`;
export const DEMO_URL = `http://127.0.0.1:${DEMO_PORT}`;
