// 本機看畫面與設計稿比對用的模擬後端（Story 3.5）：送出成員版的建置結果（web/dist，帶和正式環境同一份
// 安全標頭），並模擬登入相關的四個端點（/api/v1/me、/api/v1/auth/fake-login 的 GET 與 POST、
// /api/v1/auth/logout），回應的形狀照 openapi.json。不需要資料庫、不需要 docker。
// 畫面是真正的建置結果；這支只代替後端，不是給測試用的（Playwright 測試用 e2e/api-mock.ts 的 page.route）。
//
// 用法（在 web/ 底下，先 npm run build:member）：
//   node scripts/mock-api.mjs --port 4180
//   然後打開 http://127.0.0.1:4180/login
//
//   --port          埠，預設 4180
//   --dist          要送出的資料夾，預設 dist
//   --member-name   測試成員的顯示名稱，預設 Test Member A
//   --signed-in     一開始就是登入狀態（設計稿比對時，每次都是新的瀏覽器，沒有 cookie）
//
// 切換情境（例如看登入失敗、登出失敗）：打開或 curl
//   http://127.0.0.1:4180/__mock?login=failed            登入：ok／failed（伺服器錯誤）／denied（不在名單裡）
//   http://127.0.0.1:4180/__mock?logout=failed           登出：ok／failed
//   http://127.0.0.1:4180/__mock?loginDelay=1500         登入要等幾毫秒（看「登入中…」）
//   http://127.0.0.1:4180/__mock?logoutDelay=5000        登出要等幾毫秒（看「登出中…」）
//   http://127.0.0.1:4180/__mock?signedIn=false          登出（或 true：登入）
//   http://127.0.0.1:4180/__mock?expire=1                讓目前的登入過期（下一次換頁回到登入頁）
// 參數可以一起給；回傳目前的情境（JSON）。
/* global process, URL, setTimeout */
import { randomUUID } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));
const SECURITY_HEADERS = JSON.parse(
  readFileSync(new URL("../../radar/entrypoints/api/security_headers.json", import.meta.url), "utf-8"),
);
const COOKIE = "mock_session";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".json": "application/json",
};

function parseArgs(argv) {
  const args = { port: 4180, dist: "dist", memberName: "Test Member A", signedIn: false };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--signed-in") {
      args.signedIn = true;
      continue;
    }
    const value = argv[i + 1];
    if (key === "--port") args.port = Number(value);
    else if (key === "--dist") args.dist = value;
    else if (key === "--member-name") args.memberName = value;
    else throw new Error(`unknown option: ${key}`);
    i += 1;
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const DIST = resolve(WEB_ROOT, args.dist);
const MEMBERS = [
  { member_id: "00000000-0000-4000-8000-00000000000a", display_name: args.memberName },
  { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" },
];
const scenario = { login: "ok", logout: "ok", loginDelay: 0, logoutDelay: 0, alwaysSignedIn: args.signedIn };
/** 有效的 session token → 成員。 */
const sessions = new Map();

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Cache-Control": "no-store", ...headers });
  res.end(body);
}

function json(res, status, value, headers = {}) {
  send(res, status, JSON.stringify(value), { "Content-Type": "application/json", ...headers });
}

const TITLES = { 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 500: "Internal Server Error" };

function problem(res, status, code) {
  json(res, status, { type: "about:blank", title: TITLES[status], status, code }, {
    "Content-Type": "application/problem+json",
  });
}

function cookieToken(req) {
  const header = req.headers.cookie ?? "";
  const match = header.split(/;\s*/).find((part) => part.startsWith(`${COOKIE}=`));
  return match === undefined ? null : match.slice(COOKIE.length + 1);
}

function currentMember(req) {
  if (scenario.alwaysSignedIn) return MEMBERS[0];
  const token = cookieToken(req);
  return token === null ? null : (sessions.get(token) ?? null);
}

/** 後端的 safe_next_path 的簡化版：只收站內路徑。 */
function safeNext(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\")
    ? value
    : "/";
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

async function api(req, res, path) {
  if (req.method === "GET" && path === "/api/v1/me") {
    const member = currentMember(req);
    return member === null ? problem(res, 401, "not_authenticated") : json(res, 200, member);
  }
  if (req.method === "GET" && path === "/api/v1/auth/fake-login") {
    return json(res, 200, { mode: "pick_member", members: MEMBERS });
  }
  if (req.method === "POST" && path === "/api/v1/auth/fake-login") {
    const body = await readJson(req);
    // 結果在收到請求時就決定；等待中瀏覽器關掉了就什麼都不改（不然會影響下一次比對）。
    const outcome = scenario.login;
    await sleep(scenario.loginDelay);
    if (res.destroyed || req.socket.destroyed) return undefined;
    if (outcome === "failed") return problem(res, 500, "internal_error");
    const member = MEMBERS.find((candidate) => candidate.member_id === body.member_id);
    if (outcome === "denied" || member === undefined) return problem(res, 403, "login_rejected");
    const token = randomUUID();
    sessions.set(token, member);
    return json(res, 200, { next: safeNext(body.next) }, {
      "Set-Cookie": `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax`,
    });
  }
  if (req.method === "POST" && path === "/api/v1/auth/logout") {
    const outcome = scenario.logout;
    await sleep(scenario.logoutDelay);
    if (res.destroyed || req.socket.destroyed) return undefined;
    if (outcome === "failed") return problem(res, 500, "internal_error");
    const token = cookieToken(req);
    if (token !== null) sessions.delete(token);
    scenario.alwaysSignedIn = false;
    return send(res, 204, "", { "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax` });
  }
  return problem(res, 404, "not_found");
}

function control(res, query) {
  for (const key of ["login", "logout"]) {
    if (query.has(key)) scenario[key] = query.get(key);
  }
  for (const key of ["loginDelay", "logoutDelay"]) {
    if (query.has(key)) scenario[key] = Number(query.get(key));
  }
  if (query.has("signedIn")) scenario.alwaysSignedIn = query.get("signedIn") === "true";
  if (query.has("expire")) {
    sessions.clear();
    scenario.alwaysSignedIn = false;
  }
  json(res, 200, { ...scenario, sessions: sessions.size });
}

function staticFile(res, path) {
  let decoded;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return send(res, 400, "bad path");
  }
  const file = normalize(join(DIST, decoded));
  const inside = file.startsWith(`${DIST}/`);
  const exists = inside && (() => {
    try {
      return statSync(file).isFile();
    } catch {
      return false;
    }
  })();
  // 找不到的路徑回頁面殼（ADR-0010 第 11 點），由瀏覽器裡的路由決定顯示什麼。
  const target = exists ? file : join(DIST, "index.html");
  const type = TYPES[extname(target)] ?? "application/octet-stream";
  send(res, 200, readFileSync(target), { ...SECURITY_HEADERS, "Content-Type": type });
}

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  if (url.pathname === "/__mock") return control(res, url.searchParams);
  if (url.pathname.startsWith("/api/")) {
    api(req, res, url.pathname).catch((error) => {
      process.stderr.write(`${error}\n`);
      problem(res, 500, "internal_error");
    });
    return undefined;
  }
  return staticFile(res, url.pathname);
}).listen(args.port, "127.0.0.1", () => {
  process.stdout.write(`mock api: http://127.0.0.1:${args.port}/login (dist: ${DIST})\n`);
});
