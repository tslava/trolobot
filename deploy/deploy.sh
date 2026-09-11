#!/usr/bin/env bash
# Выполняется на хосте: приезжает по SSH через `bash -s -- <tag>` (см.
# deploy/deploy-only.sh и .github/workflows/deploy.yml), сам скрипт никогда
# не сохраняется на хосте отдельным файлом — так хост всегда исполняет
# актуальную версию из репозитория.
#
# Пишет IMAGE_TAG/IMAGE_TAG_PREV в deploy.env, поднимает docker compose,
# ждёт healthy и откатывается на предыдущий тег, если контейнер не поднялся.
set -euo pipefail

APP_DIR="/opt/trolobot"
ENV_FILE="deploy.env"
SERVICE="bot"
HEALTH_TIMEOUT_SEC=60

usage() {
  echo "usage: deploy.sh <image-tag>  (например: sha-abc1234)" >&2
  exit 1
}

TAG="${1:-}"
[ -n "$TAG" ] || usage

cd "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "deploy.sh: $APP_DIR/$ENV_FILE не найден — сначала прогоните deploy/setup-host.sh" >&2
  exit 1
fi

# Текущий тег до перезаписи — на него откатываемся при неудаче. Пустой значит
# первый деплой (в deploy.env ещё нет строки IMAGE_TAG=) — откатывать некуда.
PREV_TAG="$(grep -E '^IMAGE_TAG=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
GHCR_OWNER_LINE="$(grep -E '^GHCR_OWNER=' "$ENV_FILE" || echo 'GHCR_OWNER=tslava')"

write_deploy_env() {
  # $1 = IMAGE_TAG, $2 = IMAGE_TAG_PREV
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  {
    echo "$GHCR_OWNER_LINE"
    echo "IMAGE_TAG=$1"
    echo "IMAGE_TAG_PREV=$2"
  } >"$tmp"
  mv "$tmp" "$ENV_FILE"
}

# Общая точка отката: вызывается и при падении pull/up -d, и при таймауте
# healthcheck, чтобы IMAGE_TAG в deploy.env не оставался на непойманном
# неудачном теге ни при каком из путей отказа.
rollback() {
  # $1 = причина (для лога)
  echo "deploy.sh: $1 — откатываюсь на ${PREV_TAG:-<нет>}" >&2

  if [ -z "$PREV_TAG" ]; then
    echo "deploy.sh: предыдущего IMAGE_TAG нет (первый деплой) — откатывать некуда" >&2
    exit 1
  fi

  # Логи снимаем ДО пересоздания: compose up -d удаляет упавший контейнер,
  # и после него docker logs скажет «No such container».
  local failed_cid
  failed_cid="$(docker compose --env-file "$ENV_FILE" ps -aq "$SERVICE" | head -1 || true)"
  if [ -n "$failed_cid" ]; then
    echo "deploy.sh: последние 50 строк логов упавшего контейнера ($TAG):" >&2
    docker logs --tail 50 "$failed_cid" >&2 || true
  fi

  write_deploy_env "$PREV_TAG" "$PREV_TAG"
  if ! docker compose --env-file "$ENV_FILE" up -d; then
    echo "deploy.sh: откат на $PREV_TAG тоже не поднялся (docker compose up -d упал)" >&2
  fi

  exit 1
}

wait_healthy() {
  local cid elapsed=0 status
  cid="$(docker compose --env-file "$ENV_FILE" ps -q "$SERVICE" || true)"
  if [ -z "$cid" ]; then
    return 1
  fi
  while [ "$elapsed" -lt "$HEALTH_TIMEOUT_SEC" ]; do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "unknown")"
    if [ "$status" = "healthy" ]; then
      return 0
    fi
    if [ "$status" = "unhealthy" ]; then
      # Не ждём оставшийся таймаут, если docker уже решил, что контейнер плох.
      return 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

echo "deploy.sh: раскатываю $TAG (предыдущий тег: ${PREV_TAG:-<нет>})"
write_deploy_env "$TAG" "$PREV_TAG"

if ! docker compose --env-file "$ENV_FILE" pull; then
  rollback "docker compose pull $TAG провалился"
fi

if ! docker compose --env-file "$ENV_FILE" up -d; then
  rollback "docker compose up -d для $TAG провалился"
fi

if wait_healthy; then
  echo "deploy.sh: $TAG healthy, готово"
  exit 0
fi

rollback "$TAG не стал healthy за ${HEALTH_TIMEOUT_SEC}s"
