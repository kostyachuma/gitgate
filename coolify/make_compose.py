#!/usr/bin/env python3
"""Собирает самодостаточный docker-compose.yaml для Coolify из proxy/gitgate.py.

Код прокси вшивается в dockerfile_inline (heredoc), чтобы в Coolify можно было
вставить один файл без отдельного git-репозитория. В compose `$` удваивается.
Запуск: python3 coolify/make_compose.py > coolify/docker-compose.yaml
"""
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parent.parent
code = (root / "proxy" / "gitgate.py").read_text()
assert "PYEOF" not in code
code = code.replace("$", "$$")
body = "\n".join(("        " + line) if line else "" for line in code.splitlines())

HEAD = """\
# Сгенерировано coolify/make_compose.py из proxy/gitgate.py. Не править вручную.
# Переменные задаются в Coolify (Environment Variables): GITGATE_TOKEN (секрет), GITGATE_ALLOW.
services:
  gitgate:
    build:
      context: .
      dockerfile_inline: |
        FROM python:3.13-alpine
        RUN adduser -D -u 10001 gitgate
        COPY <<'PYEOF' /app/gitgate.py
"""
TAIL = """
        PYEOF
        USER gitgate
        CMD ["python3", "-I", "/app/gitgate.py"]
    restart: unless-stopped
    environment:
      - GITGATE_TOKEN=${GITGATE_TOKEN}
      - GITGATE_ALLOW=${GITGATE_ALLOW}
    healthcheck:
      test: ["CMD", "python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), 2)"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      hermes:
        aliases:
          - gitgate

networks:
  hermes:
    name: yuvdjreetabc8yxgqvdfj0go
    external: true
"""
sys.stdout.write(HEAD + body + TAIL)
