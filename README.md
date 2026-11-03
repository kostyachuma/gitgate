# pihost-git-sync

`gitgate` — маленький git-прокси (только стандартная библиотека Python), который стоит между ботом
(Hermes) и GitHub. Токен записи есть только у прокси, у бота его нет.

- fetch и clone проходят для репозиториев из `GITGATE_ALLOW` (например `kostyachuma/*`);
- push проходит, только если все обновляемые ссылки подходят под `refs/heads/hermes/*`;
- `main`, теги, чужие префиксы, push-cert и т. п. отклоняются, причина видна в выводе git
  (`! [remote rejected] … (gitgate: only refs/heads/hermes/* can be pushed)`).

Нужен для GitHub Free: у приватных репозиториев там нет branch protection.

## Состав

| Путь | Что это |
|---|---|
| `proxy/gitgate.py` | сам прокси |
| `proxy/test_gitgate.py` | юнит-тесты и сквозные тесты с поддельным upstream |
| `proxy/e2e_realgit.py` | проверка настоящим `git` на поддельном GitHub |
| `proxy/Dockerfile` | образ (для запуска вне Coolify) |
| `coolify/make_compose.py` | собирает самодостаточный `docker-compose.yaml` для Coolify |
| `coolify/docker-compose.yaml` | результат; код прокси вшит в `dockerfile_inline` |
| `NOTES.md` | заметки по дизайну и состояние развёртывания |

## Настройки (переменные окружения)

| Переменная | Значение |
|---|---|
| `GITGATE_TOKEN` / `GITGATE_TOKEN_FILE` | токен GitHub (fine-grained: Contents и Pull requests read/write, без Workflows) |
| `GITGATE_ALLOW` | маски `owner/repo` через запятую; пусто — всё запрещено |
| `GITGATE_REF_REGEX` | разрешённые ссылки для push (по умолчанию `^refs/heads/hermes/…`) |
| `GITGATE_REF_HINT` | текст подсказки в отказе (по умолчанию `refs/heads/hermes/*`) |
| `GITGATE_PORT` | порт, 8080 |

## Развёртывание в Coolify

1. `python3 coolify/make_compose.py > coolify/docker-compose.yaml` (после любой правки `gitgate.py`).
2. Coolify → New Resource → Docker Compose (без git-репозитория), вставить содержимое файла.
3. Переменные `GITGATE_TOKEN` (секрет) и `GITGATE_ALLOW`; домен не задавать.
4. Сеть Hermes (`yuvdjreetabc8yxgqvdfj0go`) подключена как внешняя в самом compose, под именем `gitgate`.
5. В воркспейсе: `git remote set-url origin http://gitgate:8080/<owner>/<repo>.git`.

## Проверка

```bash
cd proxy && python3 -I -m unittest      # юнит-тесты
python3 proxy/e2e_realgit.py            # настоящий git, поддельный GitHub
```

## Что прокси не делает

- Не открывает PR (только git smart HTTP).
- Не различает force-push внутри разрешённых веток: свои ветки бот переписывать может.
- Один токен на все `GITGATE_ALLOW`; для нескольких владельцев нужна доработка.
