#!/usr/bin/env bash
# 冒煙測試：用 compose.yaml 把 db 與 api 跑起來，確認 /healthz、/readyz 都回 200。
#
# 用法：.github/scripts/smoke.sh <映像> <預期架構 amd64|arm64>
#
# - 先確認映像架構＝預期架構＝這台機器的架構（雙架構要在原生執行機上跑，不用模擬器）。
# - 用獨立的 Compose 專案名稱 radar-smoke，不碰本機的 research-radar 專案；
#   --env-file /dev/null 讓 Compose 不讀 repo 根目錄的 .env。不需要任何 API key。
# - 另外確認 api 的記憶體上限是 350 MiB，以及 stop 後退出碼 0、日誌有「正常關機」。
# - 不論成功或失敗，結束時一定 down -v；失敗時先印出 db 與 api 的日誌。
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "用法：$0 <映像> <預期架構 amd64|arm64>" >&2
  exit 2
fi

IMAGE=$1
EXPECTED_ARCH=$2
PROJECT=radar-smoke
# compose.yaml 的 api 沒寫 image:，Compose 會把它的映像叫做「<專案名稱>-api」。
COMPOSE_IMAGE="${PROJECT}-api"
API_URL=http://127.0.0.1:8000
API_MEMORY_LIMIT_BYTES=$((350 * 1024 * 1024))
STARTUP_TIMEOUT_SECONDS=60

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
compose() {
  docker compose --env-file /dev/null --project-name "${PROJECT}" \
    --project-directory "${ROOT}" --file "${ROOT}/compose.yaml" "$@"
}

fail() {
  echo "冒煙測試失敗：$*" >&2
  exit 1
}

cleanup() {
  local status=$?
  if [[ ${status} -ne 0 ]]; then
    echo "----- db 日誌 -----" >&2
    compose logs --no-color db >&2 || true
    echo "----- api 日誌 -----" >&2
    compose logs --no-color api >&2 || true
  fi
  compose down --volumes --remove-orphans || true
  docker image rm "${COMPOSE_IMAGE}" >/dev/null 2>&1 || true
  exit "${status}"
}

case "${EXPECTED_ARCH}" in
  amd64 | arm64) ;;
  *) echo "預期架構只能是 amd64 或 arm64：${EXPECTED_ARCH}" >&2; exit 2 ;;
esac

case "$(uname -m)" in
  x86_64 | amd64) HOST_ARCH=amd64 ;;
  aarch64 | arm64) HOST_ARCH=arm64 ;;
  *) HOST_ARCH=$(uname -m) ;;
esac

IMAGE_PLATFORM=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE}")
echo "映像 ${IMAGE}：${IMAGE_PLATFORM}；這台機器：linux/${HOST_ARCH}；預期：linux/${EXPECTED_ARCH}"
[[ "${IMAGE_PLATFORM}" == "linux/${EXPECTED_ARCH}" ]] || fail "映像架構 ${IMAGE_PLATFORM} 不是 linux/${EXPECTED_ARCH}"
[[ "${HOST_ARCH}" == "${EXPECTED_ARCH}" ]] || fail "這台機器是 ${HOST_ARCH}，不是原生的 ${EXPECTED_ARCH}"

trap cleanup EXIT

docker tag "${IMAGE}" "${COMPOSE_IMAGE}"
compose pull --quiet db
compose up --detach --no-build --pull never --wait db api

http_status() {
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "$1" || true
}

echo "等待 api 起來……"
deadline=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
until [[ "$(http_status "${API_URL}/healthz")" == 200 ]]; do
  [[ -n "$(compose ps --status running --quiet api)" ]] || fail "api 容器沒有在跑"
  ((SECONDS < deadline)) || fail "api 沒有在 ${STARTUP_TIMEOUT_SECONDS} 秒內回應 /healthz"
  sleep 1
done

for path in /healthz /readyz; do
  status=$(http_status "${API_URL}${path}")
  echo "${path} → ${status}"
  [[ "${status}" == 200 ]] || fail "${path} 回 ${status}，不是 200"
done

API_CONTAINER=$(compose ps --quiet api)
memory=$(docker inspect --format '{{.HostConfig.Memory}}' "${API_CONTAINER}")
echo "api 記憶體上限：${memory} bytes"
[[ "${memory}" == "${API_MEMORY_LIMIT_BYTES}" ]] || fail "api 記憶體上限是 ${memory}，不是 350 MiB"

compose stop api
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "${API_CONTAINER}")
echo "api 停止後的退出碼：${exit_code}"
[[ "${exit_code}" == 0 ]] || fail "api 停止後的退出碼是 ${exit_code}，不是 0"
# 先存起來再找：grep -q 找到就提早結束，接在管線後面會讓 pipefail 誤判失敗。
api_logs=$(compose logs --no-color api)
[[ "${api_logs}" == *正常關機* ]] || fail "api 的日誌沒有「正常關機」"

echo "冒煙測試通過（linux/${EXPECTED_ARCH}）"
