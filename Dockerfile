# api 與 worker 共用的映像；compose 用 command 決定跑哪一個。
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

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
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie
RUN groupadd --system --gid 10001 radar \
    && useradd --system --uid 10001 --gid radar --no-create-home radar
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# radar migrate 與 api 的 /readyz 以工作目錄下的 migrations/ 找遷移檔。
COPY migrations ./migrations
USER radar
EXPOSE 8000
CMD ["python", "-m", "radar.entrypoints.api"]
