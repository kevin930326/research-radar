# api 與 worker 共用的映像；compose 用 command 決定跑哪一個。
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# 成員版前端（Story 3.1、ADR-0036）：在建置機的原生架構上建（產物是靜態檔，和映像架構無關），
# 最後一段只複製 web/dist。安全標頭檔是 vite.config.ts 讀的唯一來源，所以一起複製。
FROM --platform=$BUILDPLATFORM node:26-trixie-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY radar/entrypoints/api/security_headers.json /radar/entrypoints/api/security_headers.json
COPY web/ ./
RUN npm run build:member

# 備份與還原（Story 3.18、ADR-0051）：PostgreSQL 官方套件庫（PGDG）的簽章金鑰。在這一段下載並驗
# 指紋（gpg 只裝在這一段，不進最後的映像），最後一段只複製驗過的金鑰檔。不執行任何下載來的腳本。
# 指紋直接寫在 RUN 裡（不用建置參數，建置時換不掉）；和 .github/workflows/ci.yml 的同一個值，
# tests/arch/test_structure.py 檢查兩邊一樣。
FROM python:3.14-slim-trixie AS pgdg-key
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location --proto '=https' \
        --output /pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && gpg --show-keys --with-colons /pgdg.asc > /pgdg-keys.txt \
    && test "$(grep -c '^pub:' /pgdg-keys.txt)" = 1 \
    && test "$(awk -F: '$1 == "fpr" { print $10; exit }' /pgdg-keys.txt)" \
        = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"

FROM python:3.14-slim-trixie AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# 先只裝相依套件，原始碼改了這一層的快取還能用。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY radar ./radar
# --reinstall-package radar：每次都從上面複製進來的原始碼重新打包。
# 不加的話，uv 只看 pyproject.toml 有沒有變來決定要不要重包；快取掛載裡有舊的包時，
# 程式改了、pyproject.toml 沒改，映像裡就會是舊程式（2026-11-14 第一次匯入時發生過）。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package radar

FROM python:3.14-slim-trixie
RUN groupadd --system --gid 10001 radar \
    && useradd --system --uid 10001 --gid radar --no-create-home radar
# radar backup／restore 用的系統工具（Story 3.18、ADR-0051，Kevin 2026-11-18 同意只加這兩個）：
# - PostgreSQL 18 的用戶端（pg_dump、pg_restore、psql）：PGDG 的封存套件庫（舊的小版本也一直留著，
#   釘住的版本不會因為新版發佈而消失）。只裝用戶端，不裝伺服器。
# - age：Debian trixie 官方套件庫。
# 版本都釘住；升級時改這裡的版本號（ADR-0051）。
COPY --from=pgdg-key /pgdg.asc /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
RUN echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
        "https://apt-archive.postgresql.org/pub/repos/apt trixie-pgdg-archive main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        postgresql-client-18=18.7-1.pgdg13+2 \
        libpq5=18.7-1.pgdg13+2 \
        age=1.2.1-1+b5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
# /usr/lib/postgresql/18/bin 放在 /usr/bin 前面：直接用 18 版的執行檔，不經 Debian 的版本選擇包裝。
ENV PATH=/app/.venv/bin:/usr/lib/postgresql/18/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# radar migrate 與 api 的 /readyz 以工作目錄下的 migrations/ 找遷移檔。
COPY migrations ./migrations
# 領域設定（期刊清單、OpenAlex 設定）：api、worker、CLI 啟動時驗證，預設讀工作目錄下的 config/。
COPY config ./config
# LLM 工作的 prompt 檔與雜湊登記（AD-8、ADR-0030）：閘門預設讀工作目錄下的 prompts/。
COPY prompts ./prompts
# 成員版前端的建置結果：api 以工作目錄下的 web/dist 送出頁面殼與靜態檔（ADR-0036）。
COPY --from=web /web/dist ./web/dist
USER radar
EXPOSE 8000
CMD ["python", "-m", "radar.entrypoints.api"]
