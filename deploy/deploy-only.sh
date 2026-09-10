#!/usr/bin/env bash
# Forced command для ключа деплоя в ~bot/.ssh/authorized_keys:
#   command="/opt/trolobot/deploy-only.sh",no-port-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
#
# CI подключается как `ssh bot@host "bash -s -- <tag>" < deploy/deploy.sh` —
# именно эта строка (без содержимого stdin) попадает в SSH_ORIGINAL_COMMAND.
# Разрешена ровно форма "bash -s -- sha-<7-12 hex>", всё остальное — отказ.
set -euo pipefail

PATTERN='^bash -s -- sha-[0-9a-f]{7,12}$'
CMD="${SSH_ORIGINAL_COMMAND:-}"

if [[ "$CMD" =~ $PATTERN ]]; then
  # Намеренно без кавычек: нужно разбить "bash -s -- sha-xxxxx" на слова,
  # чтобы bash -s прочитал скрипт (deploy.sh) из stdin, а sha-xxxxx ушёл в $1.
  # shellcheck disable=SC2086
  exec $CMD
fi

echo "deploy-only.sh: запрещённая команда: '${CMD:-<пусто>}'" >&2
command -v logger >/dev/null 2>&1 && logger -t trolobot-deploy "rejected command: ${CMD:-<empty>}"
exit 1
