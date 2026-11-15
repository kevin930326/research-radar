// i18next 初始化（AD-21）：介面文字只放 zh-TW/ 底下的 JSON，一個命名空間一個檔；
// 同步載入、不做語言偵測。
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import ai from "./zh-TW/ai.json";
import caveat from "./zh-TW/caveat.json";
import common from "./zh-TW/common.json";
import notice from "./zh-TW/notice.json";

export const LANGUAGE = "zh-TW";

export const resources = {
  [LANGUAGE]: { common, notice, caveat, ai },
} as const;

export const NAMESPACES = ["common", "notice", "caveat", "ai"] as const;

void i18n.use(initReactI18next).init({
  lng: LANGUAGE,
  fallbackLng: false,
  resources,
  ns: NAMESPACES,
  defaultNS: "common",
  // React 本身會跳脫輸出，這裡不再跳脫一次。
  interpolation: { escapeValue: false },
  initAsync: false,
  returnNull: false,
});

export default i18n;
