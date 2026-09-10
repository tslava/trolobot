# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

# ---- builder: собираем виртуальное окружение --------------------------------
FROM python:3.12-slim AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync --frozen --no-dev

# ---- runtime: минимальный образ без сборочных инструментов ------------------
FROM python:3.12-slim AS runtime

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --system --no-create-home bot

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot pyproject.toml uv.lock ./
COPY --chown=bot:bot src ./src
COPY --chown=bot:bot config.yaml few_shot.yaml ./
COPY --chown=bot:bot prompts ./prompts

# /app/data — точка монтирования bind-volume из docker-compose.yml (см. README:
# требуется chown 10001:10001 на хосте перед первым запуском).
RUN mkdir -p /app/data && chown -R bot:bot /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

USER bot

# Ничего не слушает наружу — EXPOSE не нужен (long polling).
# Токен читается из переменной окружения контейнера и никогда не попадает в лог.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, sys, httpx\nurl = 'https://api.telegram.org/bot' + os.environ['BOT_TOKEN'] + '/getMe'\nr = httpx.get(url, timeout=5.0)\nsys.exit(0 if r.status_code == 200 else 1)"]

CMD ["uv", "run", "--no-sync", "python", "-m", "trolobot"]
