# pihost-git-sync: заметки по дизайну (2026-11-03)

Статус: обсуждение, ничего не установлено. Файлы на хост не ставились, `/etc` не менялся.
Основной вариант с 2026-11-03: git-прокси `gitgate` (раздел ниже). Схема с хостовым скриптом и inotify
из разделов «Решения» и «Идея» осталась запасной.

## Основной вариант: git-прокси gitgate (Coolify)

Токен записи живёт только в прокси. Агент ходит на `http://gitgate:8080/<owner>/<repo>.git` без учётных данных.

- fetch (`info/refs`, `git-upload-pack`) пропускается для репозиториев из `GITGATE_ALLOW` (например `kostyachuma/*`).
- push (`git-receive-pack`) пропускается, только если ВСЕ ссылки в запросе подходят под `refs/heads/hermes/...`.
  Команды обновления читаются из начала запроса (pkt-line до `0000`), потом поток идёт дальше как есть.
  Отклоняются: `main`, теги, чужие префиксы, push-cert, сжатое тело, `..`, `.lock`.
- Остальные пути: 404/403. Пустой `GITGATE_ALLOW` запрещает всё.
- Локальный git у агента работает целиком (stash, rebase, reset, amend, worktree). Ограничен только сетевой push.
- Токен: fine-grained PAT, Contents и Pull requests read/write, БЕЗ Workflows (иначе GitHub сам отклонит правку
  `.github/workflows` в ветке агента, и Actions с секретами через ветку не запустить).
- Hermes делает свой `git fetch` перед worktree без изменений, таймер не нужен.
- PR прокси не открывает. Вариант: отдельный эндпоинт позже, либо кнопка «Compare & pull request» на GitHub.

Код: `proxy/gitgate.py` (только stdlib), `proxy/test_gitgate.py` (11 тестов, проходят: разбор команд, фильтр,
сквозной проход через поддельный upstream, большой push 200 КБ целиком, отказ без пересылки), `proxy/Dockerfile`.
Проверено 2026-11-04 на развёрнутом в Coolify gitgate настоящим git (shallow-клон tarotgo из контейнера Hermes):
push `hermes/gitgate-test` прошёл, push в `main`, `agent/x`, тег и смешанный push (`hermes/*` + `main`) получили 403 и ничего не создали,
удаление тестовой ветки через прокси прошло. Тестовая ветка и клон удалены.
Доработка 2026-11-04 (в Coolify ещё НЕ развёрнута, нужен новый compose + Deploy): отказ приходит как ответ receive-pack
(`ng <ref> gitgate: only refs/heads/hermes/* can be pushed` + sideband), git показывает `! [remote rejected] … (причина)`.
Найдена и исправлена ошибка: для push больше 1 МБ git сначала шлёт пробный запрос `0000` без команд, прокси отвечал на него 403,
и такие push не работали. Теперь пустой пробный запрос пропускается. Проверка настоящим git: `proxy/e2e_realgit.py`
(push 3 МБ в `hermes/ok` проходит, `main`/`agent/x`/тег/смешанный push отклоняются с причиной). Юнит-тестов 18.

Готовый файл для Coolify: `coolify/docker-compose.yaml` (генерируется `coolify/make_compose.py`, код прокси вшит
в `dockerfile_inline`, сеть `yuvdjreetabc8yxgqvdfj0go` внешняя, алиас `gitgate`). Проверено 2026-11-03: `docker compose config`
проходит, образ собирается, хэш `/app/gitgate.py` в образе совпадает с исходником, в контейнере 404 на `/` и 403 на чужого владельца.
Тестовый образ удалён. Развёртывание в Coolify не сделано.

Развёртывание в Coolify (старый вариант через git-репозиторий, не нужен):
1. Приложение из репозитория `pihost-git-sync`, base directory `proxy`, Dockerfile.
2. Секретная переменная `GITGATE_TOKEN`, `GITGATE_ALLOW=kostyachuma/*`. В контейнеры Hermes токен не передавать.
3. Порт наружу не публиковать. Подключить к сети стека Hermes (Connect to Predefined Network) или положить в тот же compose.
4. В каждом репозитории воркспейса: `git remote set-url origin http://gitgate:8080/kostyachuma/<repo>.git`,
   убрать `core.sshCommand`.
5. Проверка: fetch проходит, push в `hermes/test` проходит, push в `main` получает 403 и до GitHub не доходит.

## Задача

Hermes WebUI на `pihost` работает с несколькими репозиториями (`autohome`, будущий `tarotgo`, другие).
Бот должен коммитить в ветки `hermes/*`, но не мог менять `main`.
Ветка `main` не protected: GitHub Free не даёт защиту для приватных репозиториев
(документация: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
Локальные хуки обходятся (`--no-verify`, правка хуков), надёжно только отсутствие ключа записи у бота.

## Что выяснено про текущее состояние

- `/workspace` в контейнерах Hermes: том `hermes-workspace`. WebUI и agent видят его по-разному (`/workspace`, а `~/.hermes` webui = `/opt/data` у agent).
- `autohome`: remote `git@github.com:kostyachuma/autohome.git`, в `.git/config` задан `core.sshCommand` с ключом
  `/home/hermes/.ssh/autohome_deploy`. Этого файла нет ни в одном контейнере, `git ls-remote` падает: fetch и push не работают.
- По `SOUL.md` профиля `autohome` раньше была «host job»: каждые 5 минут обновляла `origin/*`, пушила `agents/*`
  (после `pre-push` typecheck) и открывала PR с `Fixes AUT-…`. На этом сервере её нет (проверено с root: cron, systemd, /root).
- В Hermes нет защиты ветки или белого списка веток для push. Есть флаг `HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE=1`
  (commit/push/pull/checkout из WebUI, всё или ничего) и обычные git-хуки.
- Перед созданием worktree Hermes сам делает `git fetch origin <branch>` (таймаут 5 с, пропускается, если `FETCH_HEAD`
  моложе окна свежести). Если fetch не удался, это не ошибка: worktree стартует от старого `origin/main`.
  Источник: `/app/api/worktrees.py` (`_resolve_worktree_base`).

## Решения

- Префикс веток бота: `hermes/*` (так Hermes сам называет ветки worktree, `hermes/<id>`). Решение от 2026-11-04, раньше было `agent/*`.
- Push и создание PR делает только хост. Токен записи лежит на хосте, бота у него нет.
- Токен: fine-grained PAT, доступ ко всем репозиториям, Contents и Pull requests read/write, без срока
  (если GitHub даст). Возможный апгрейд: GitHub App (ключ не протухает, токены на час).
- Без `gh`: fetch и push обычным `git` по HTTPS, токен через `GIT_CONFIG_*`/`http.extraHeader`, PR через `curl` + `jq`.
- Скрипт вызывает git в обход `.git/config` и хуков репозитория (`core.hooksPath=/dev/null`), URL и ключ берёт
  из своего конфига: иначе агент может подменить хук или remote.
- Пушить только явный refspec `refs/heads/hermes/*`, никогда `main`, без force.
- Триггер push и PR: `inotifywait -r` на `.git/refs/heads/hermes` и `packed-refs` (inotifywait на хосте есть,
  `max_user_watches` = 131021). Дебаунс 3–5 с, `flock` на репозиторий, `StartLimitBurst`.
  `PathChanged` в systemd не подходит: не смотрит вложенные каталоги.
- Оформить как отдельный репозиторий `pihost-git-sync` (идемпотентный `install.sh`, `uninstall.sh`, systemd-юниты,
  `repos.conf` и `token` в `.gitignore`).

## Идея, к которой хочется вернуться: fetch без таймера

Если дать контейнеру Hermes **доступ только на чтение**, то fetch перед созданием ветки сделает сам Hermes
(он это уже умеет), и таймер для fetch не нужен. Остаётся событийный push/PR на хосте и редкий страховочный
таймер (раз в час).

Варианты read-only доступа в контейнере:
1. Read-only deploy key на каждый репозиторий: минимальный риск, но на каждый новый проект нужно добавить ключ в GitHub.
2. Один read-only fine-grained токен на все репозитории: ничего добавлять не надо, но бот сможет читать все
   приватные репозитории (риск утечки исходников, `main` не страдает).

Открытый вопрос: насколько критично чтение чужих репозиториев ботом. Решение не принято.

## Что нужно, чтобы продолжить

1. Монорепа `tarotgo` на GitHub: `логин/имя`, приватная ли.
2. Fine-grained токен (без срока) положить в файл на хосте (путь согласуем, в чат не присылать).
3. `sudo -v` перед установкой в `/etc` и `/usr/local`.
4. Решить: PR автоматически (как в `autohome`) или только push веток; git-личность бота для `tarotgo`
   (сейчас `Katana <katana@autoho.me>`); чинить ли `autohome` тем же скриптом.
5. Отдельное «да» на глобальные настройки Hermes: `worktree: true` в `config.yaml` и
   `HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE=1` (нужен редеплой через Coolify).

## Другие проекты с похожей идеей (для справки)

- Edge Delta: git в песочнице нет, ветки и PR делает хост через API: https://edgedelta.com/company/blog/ai-teammate-sandbox-real-linux-box
- foundry-sandbox: прокси на хосте подставляет ключи, политика git запрещает protected-ветки и force-push:
  https://awesome.ecosyste.ms/projects/github.com%2Ffoundry-works%2Ffoundry-sandbox
- NAV: sandbox блокирует push в default-ветку, feature-ветки разрешены: https://ki-utvikling.nav.no/en/news/sandbox-confines-the-process-not-the-token
- Cloud Native Now: `.git/hooks` и `.git/config` в рабочей папке могут влиять на git на хосте:
  https://cloudnativenow.com/contributed-content/put-the-agent-in-a-box-running-claude-code-in-a-container/
