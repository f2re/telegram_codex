# Telegram AI CLI Bot

Красивый и практичный Telegram-бот для запуска локальных AI CLI-агентов:
**Gemini CLI в YOLO-режиме** и **Codex CLI без подтверждений**. Бот принимает
задачу в Telegram, запускает CLI в рабочей директории сервера, сохраняет лог и
возвращает результат обратно в чат.

> Важно: `/gemini` и `/codex` запускают инструменты в режимах с автоматическим
> подтверждением действий. Добавляйте в allowlist только тех Telegram-пользователей,
> которым вы полностью доверяете.

## Что умеет

- Работает через Telegram long polling, без публичного webhook.
- Ограничивает доступ по числовым Telegram user ID.
- Запускает только одну активную задачу одновременно.
- Хранит историю заданий в SQLite.
- Возвращает ответ в чат текстом и Markdown-документом.
- Поддерживает `/cancel` для остановки текущего процесса.
- Ставит systemd-сервис.
- Умеет устанавливаться на чистую Linux-систему.
- Сохраняет ответы установщика и продолжает повторную установку без повторного ввода.

## Архитектура

```text
Telegram chat
    |
    v
codex-telegram-bot
    |
    +-- /plan   -> codex-plan wrapper, read-only Codex
    +-- /run    -> codex-run wrapper, Codex bypass mode
    +-- /gemini -> gemini --skip-trust --yolo --prompt
    +-- /codex  -> codex exec --dangerously-bypass-approvals-and-sandbox
    |
    v
PROJECT_DIR / repo / tools / tests / commits
```

## Требования

Минимально нужна Linux-система с `systemd` и одним из пакетных менеджеров:

- `apt`
- `dnf`
- `yum`

Установщик сам подготовит:

- Python, venv, pip;
- SQLite;
- Git, curl, rsync;
- Node.js/npm, если попросите;
- `@openai/codex`, если попросите;
- `@google/gemini-cli`, если попросите;
- service user;
- рабочую директорию;
- systemd unit;
- fallback-обёртки `codex-plan` и `codex-run`.

Вручную нужно получить:

- Telegram bot token у `@BotFather`;
- числовой Telegram user ID, например через `@userinfobot`.

## Быстрая установка

```bash
sudo ./install.sh
```

Установщик задаст вопросы, красиво покажет этапы установки и сохранит ответы в:

```text
.install-state.env
```

Если установка оборвалась на установке пакетов, npm, systemd или любом другом
шаге, просто запустите её ещё раз:

```bash
sudo ./install.sh
```

Скрипт подхватит сохранённые настройки и предложит их как значения по умолчанию.
Файл `.install-state.env` добавлен в `.gitignore`, потому что там может быть
Telegram token.

## Что спросит установщик

- Linux user, от имени которого будет работать бот.
- Создавать ли этого пользователя, если его нет.
- Telegram bot token.
- Разрешённые Telegram user IDs через запятую.
- Рабочую директорию `PROJECT_DIR`.
- Создавать ли рабочую директорию.
- Ставить ли Node.js/npm.
- Ставить ли Codex CLI через npm.
- Ставить ли Gemini CLI через npm.
- Создавать ли fallback-обёртки `codex-plan` и `codex-run`.
- Пути установки, данных, env-файла и systemd unit.
- Запускать ли сервис сразу.

## Авторизация CLI

После установки CLI нужно авторизовать под тем же пользователем, от имени
которого работает systemd-сервис. Если пользователь по умолчанию `codexbot`:

```bash
sudo -iu codexbot codex login
sudo -iu codexbot gemini
sudo systemctl restart codex-telegram-bot
```

Проверить логи:

```bash
journalctl -u codex-telegram-bot -f
```

## Команды в Telegram

```text
/start
/help
/plan <задача>
/run <задача>
/gemini <задача>
/codex <задача>
/status
/jobs
/last
/cancel
```

Обычный текст без команды считается `/plan`.

Примеры:

```text
/plan Посмотри структуру репозитория. Ничего не меняй. Верни план работ и риски.
```

```text
/gemini Найди причину ошибки тестов, исправь её и кратко опиши изменения.
```

```text
/codex Проверь проект, исправь проблему сборки и верни итоговый отчёт.
```

## Где что лежит

По умолчанию:

```text
/opt/codex-telegram-bot/                 приложение
/opt/codex-telegram-bot/bin/codex-plan   wrapper для /plan
/opt/codex-telegram-bot/bin/codex-run    wrapper для /run
/var/lib/codex-telegram-bot/             база, логи, результаты
/etc/codex-telegram-bot.env              настройки сервиса
/etc/systemd/system/codex-telegram-bot.service
```

## Управление сервисом

```bash
sudo systemctl status codex-telegram-bot
sudo systemctl restart codex-telegram-bot
sudo systemctl stop codex-telegram-bot
journalctl -u codex-telegram-bot -f
```

Посмотреть последние задания:

```bash
sqlite3 /var/lib/codex-telegram-bot/bot.sqlite3 \
  "select id, mode, status, created_at, finished_at, return_code, output_file from jobs order by id desc limit 20;"
```

## Безопасность

Бот намеренно даёт Telegram-командам доступ к локальным AI CLI:

- `/gemini` запускает Gemini CLI с `--yolo`;
- `/codex` запускает Codex CLI с `--dangerously-bypass-approvals-and-sandbox`;
- `/run` через wrapper тоже запускает Codex в bypass-режиме.

Практические правила:

- держите `TELEGRAM_ALLOWED_USERS` максимально коротким;
- не запускайте сервис от `root`;
- используйте отдельного пользователя, например `codexbot`;
- не храните production-секреты в рабочей директории;
- проверяйте логи после первых запусков;
- используйте `/plan` для анализа, а `/run`, `/gemini`, `/codex` только когда
  готовы разрешить изменения.

## Повторная настройка

Изменить настройки можно двумя способами:

1. Отредактировать systemd env:

```bash
sudo nano /etc/codex-telegram-bot.env
sudo systemctl restart codex-telegram-bot
```

2. Удалить сохранённые ответы установщика и пройти вопросы заново:

```bash
rm -f .install-state.env
sudo ./install.sh
```
