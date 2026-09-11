#!/usr/bin/env bash
# Первичная настройка VPS под trolobot. Идемпотентен — можно перезапускать.
# Запускать один раз владельцем под sudo на самом хосте:
#
#   sudo bash deploy/setup-host.sh \
#     --pubkey-file /path/to/deploy_key.pub \
#     [--compose-path /path/to/docker-compose.yml] \
#     [--repo tslava/trolobot] [--ref main]
#
# Без --compose-path docker-compose.yml скачивается с GitHub raw по ветке --ref.
# Без --pubkey-file authorized_keys для деплой-ключа не трогается — придётся
# добавить строку руками (скрипт печатает готовый шаблон).
set -euo pipefail

APP_DIR="/opt/trolobot"
BOT_USER="bot"
DATA_UID=10001
DATA_GID=10001
REPO="tslava/trolobot"
REF="main"
COMPOSE_PATH=""
PUBKEY_FILE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[setup-host] $*"; }
warn() { echo "[setup-host] ВНИМАНИЕ: $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --compose-path) COMPOSE_PATH="$2"; shift 2 ;;
    --pubkey-file) PUBKEY_FILE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "запускать под sudo/root" >&2
  exit 1
fi

# ---- 1. Docker + compose plugin ---------------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "docker уже установлен, пропускаю"
else
  log "устанавливаю docker из официального репозитория"
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  CODENAME="${VERSION_CODENAME:-$(lsb_release -cs 2>/dev/null || echo noble)}"
  cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable
EOF
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# ---- 2. Пользователь bot ------------------------------------------------------
if id -u "$BOT_USER" >/dev/null 2>&1; then
  log "пользователь $BOT_USER уже существует, пропускаю"
else
  log "создаю пользователя $BOT_USER (без пароля, домашний каталог)"
  useradd --create-home --shell /bin/bash "$BOT_USER"
  passwd -l "$BOT_USER" >/dev/null   # блокируем пароль — только SSH-ключ
fi
usermod -aG docker "$BOT_USER"

# ---- 3. /opt/trolobot и data --------------------------------------------------
mkdir -p "$APP_DIR"
chown "$BOT_USER:$BOT_USER" "$APP_DIR"
chmod 750 "$APP_DIR"

mkdir -p "$APP_DIR/data"
chown "$DATA_UID:$DATA_GID" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"

mkdir -p "$APP_DIR/backups"
chown "$BOT_USER:$BOT_USER" "$APP_DIR/backups"
chmod 750 "$APP_DIR/backups"

# ---- 4. docker-compose.yml ----------------------------------------------------
if [ -f "$APP_DIR/docker-compose.yml" ]; then
  log "$APP_DIR/docker-compose.yml уже есть, не перезаписываю (удалите файл, чтобы обновить из источника)"
elif [ -n "$COMPOSE_PATH" ]; then
  log "копирую docker-compose.yml из $COMPOSE_PATH"
  cp "$COMPOSE_PATH" "$APP_DIR/docker-compose.yml"
else
  log "скачиваю docker-compose.yml из github.com/${REPO}@${REF}"
  curl -fsSL "https://raw.githubusercontent.com/${REPO}/${REF}/docker-compose.yml" \
    -o "$APP_DIR/docker-compose.yml"
fi
chown "$BOT_USER:$BOT_USER" "$APP_DIR/docker-compose.yml"

# ---- 5. deploy.sh / deploy-only.sh --------------------------------------------
# deploy.sh на хосте не хранится: CI гонит его каждый раз через stdin
# (`ssh ... "bash -s -- <tag>" < deploy/deploy.sh`), поэтому хост всегда
# исполняет актуальную версию из репозитория. На хосте нужен только
# forced-command обвязчик deploy-only.sh.
if [ -f "$SCRIPT_DIR/deploy-only.sh" ]; then
  cp "$SCRIPT_DIR/deploy-only.sh" "$APP_DIR/deploy-only.sh"
else
  log "скачиваю deploy-only.sh из github.com/${REPO}@${REF}"
  curl -fsSL "https://raw.githubusercontent.com/${REPO}/${REF}/deploy/deploy-only.sh" \
    -o "$APP_DIR/deploy-only.sh"
fi
chown "$BOT_USER:$BOT_USER" "$APP_DIR/deploy-only.sh"
chmod 750 "$APP_DIR/deploy-only.sh"

# ---- 6. .env (секреты приложения) ---------------------------------------------
if [ -f "$APP_DIR/.env" ]; then
  log "$APP_DIR/.env уже есть, не трогаю"
else
  ENV_EXAMPLE=""
  if [ -f "$SCRIPT_DIR/../.env.example" ]; then
    ENV_EXAMPLE="$SCRIPT_DIR/../.env.example"
  fi
  if [ -n "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$APP_DIR/.env"
  else
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${REF}/.env.example" \
      -o "$APP_DIR/.env"
  fi
  chown "$BOT_USER:$BOT_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  warn "$APP_DIR/.env создан пустым из .env.example — ЗАПОЛНИТЕ его руками (BOT_TOKEN, OPENROUTER_API_KEY, ...) перед первым запуском"
fi

# ---- 7. deploy.env (IMAGE_TAG / GHCR_OWNER, не секрет) ------------------------
if [ -f "$APP_DIR/deploy.env" ]; then
  log "$APP_DIR/deploy.env уже есть, не трогаю"
else
  GHCR_OWNER="$(echo "$REPO" | cut -d/ -f1)"
  cat >"$APP_DIR/deploy.env" <<EOF
GHCR_OWNER=${GHCR_OWNER}
IMAGE_TAG=latest
EOF
  chown "$BOT_USER:$BOT_USER" "$APP_DIR/deploy.env"
  chmod 644 "$APP_DIR/deploy.env"
fi

# ---- 8. SSH-ключ деплоя (authorized_keys с forced command) -------------------
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
SSH_DIR="$BOT_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
touch "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

if [ -n "$PUBKEY_FILE" ]; then
  PUBKEY="$(cat "$PUBKEY_FILE")"
  RESTRICTED_LINE="command=\"$APP_DIR/deploy-only.sh\",no-port-forwarding,no-agent-forwarding,no-pty ${PUBKEY}"
  if grep -qF "${PUBKEY#* }" "$AUTH_KEYS" 2>/dev/null; then
    log "публичный ключ уже есть в $AUTH_KEYS, пропускаю"
  else
    echo "$RESTRICTED_LINE" >>"$AUTH_KEYS"
    log "добавил ограниченный ключ деплоя в $AUTH_KEYS"
  fi
else
  warn "не передан --pubkey-file — добавьте вручную строку в $AUTH_KEYS:"
  echo "  command=\"$APP_DIR/deploy-only.sh\",no-port-forwarding,no-agent-forwarding,no-pty <содержимое deploy-ключа.pub>"
fi
chown -R "$BOT_USER:$BOT_USER" "$SSH_DIR"

# ---- 9. ufw: только SSH наружу -------------------------------------------------
if ! command -v ufw >/dev/null 2>&1; then
  apt-get install -y ufw
fi
if ufw status | grep -q "^Status: active"; then
  # Файрвол уже настроен владельцем (на хосте могут жить другие сервисы,
  # например гейтвей на 443) — правила не трогаем, только гарантируем SSH.
  ufw allow OpenSSH >/dev/null
  log "ufw уже активен — существующие правила сохранены, SSH разрешён:"
  ufw status numbered | sed 's/^/    /'
else
  ufw allow OpenSSH >/dev/null
  ufw default deny incoming >/dev/null
  ufw --force enable >/dev/null
  log "ufw: allow OpenSSH, default deny incoming, enabled"
fi

# ---- 10. sshd: проверка, без изменений (не сломать доступ владельца) ---------
SSHD_EFFECTIVE="$(sshd -T 2>/dev/null || true)"
PASS_AUTH="$(echo "$SSHD_EFFECTIVE" | awk '/^passwordauthentication/{print $2}')"
ROOT_LOGIN="$(echo "$SSHD_EFFECTIVE" | awk '/^permitrootlogin/{print $2}')"
if [ "$PASS_AUTH" = "yes" ]; then
  warn "sshd: PasswordAuthentication всё ещё yes. Если вы (владелец) заходите по паролю," \
       "заведите себе ключевой доступ, а затем выставьте 'PasswordAuthentication no' в /etc/ssh/sshd_config" \
       "и перезапустите sshd — этот скрипт это НЕ делает сам, чтобы не отрезать вам доступ."
fi
if [ "$ROOT_LOGIN" != "prohibit-password" ] && [ "$ROOT_LOGIN" != "no" ]; then
  warn "sshd: PermitRootLogin=${ROOT_LOGIN:-unknown}. Рекомендуется 'PermitRootLogin prohibit-password' в /etc/ssh/sshd_config."
fi

# ---- 11. docker login в ghcr.io (для приватного пакета) ----------------------
log "если пакет ghcr.io/${REPO} приватный, залогиньте пользователя $BOT_USER в ghcr.io:"
echo "  sudo -u $BOT_USER docker login ghcr.io -u <github-username> --password-stdin <<< \"<PAT с правом read:packages>\""
echo "  (либо сделайте пакет публичным в настройках GHCR — тогда логин не нужен)"

# ---- 12. еженедельный бэкап bot.db --------------------------------------------
if ! command -v sqlite3 >/dev/null 2>&1; then
  apt-get install -y sqlite3
fi

BACKUP_SCRIPT="$APP_DIR/backup.sh"
cat >"$BACKUP_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/trolobot
sqlite3 data/bot.db ".backup backups/bot-$(date +%F).db"
# храним последние 8 копий
ls -1t backups/bot-*.db 2>/dev/null | tail -n +9 | xargs -r rm -f
EOF
chown "$BOT_USER:$BOT_USER" "$BACKUP_SCRIPT"
chmod 750 "$BACKUP_SCRIPT"

CRON_MARKER="# trolobot weekly backup"
CRON_LINE="0 3 * * 0 $BACKUP_SCRIPT >>/opt/trolobot/backups/backup.log 2>&1 $CRON_MARKER"
EXISTING_CRON="$(crontab -u "$BOT_USER" -l 2>/dev/null || true)"
if echo "$EXISTING_CRON" | grep -qF "$CRON_MARKER"; then
  log "cron-бэкап уже настроен, пропускаю"
else
  { echo "$EXISTING_CRON"; echo "$CRON_LINE"; } | grep -v '^$' | crontab -u "$BOT_USER" -
  log "добавил еженедельный cron-бэкап (вс 03:00) от имени $BOT_USER"
fi

log "готово. Дальше руками: заполнить $APP_DIR/.env, при приватном пакете — docker login, затем push в main."
