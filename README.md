# Telegram AI CLI Bot

Minimal Telegram service that starts local AI CLI agents from Telegram and sends
their final output back to the chat.

The bot receives tasks in Telegram, starts existing `codex-plan`/`codex-run` or
direct `gemini`/`codex` CLI commands, tracks state in SQLite, and sends the
result back to Telegram.

It intentionally does **not** modify `codex-plan` or `codex-run`.

## Architecture

```text
Telegram
  ↓
codex-telegram-bot
  ↓
codex-plan / codex-run / gemini --yolo / codex exec bypass mode
  ↓
AI CLI in PROJECT_DIR
  ↓
repo / tools / tests / commits
```

## Features

- Telegram long polling, no public webhook required.
- Telegram allowlist by numeric user ID.
- Single active job per worktree for reliability.
- SQLite job history.
- `systemd` deployment.
- `/cancel` kills the current process group.
- Plain text defaults to `/plan`, not `/run`.
- Results are sent as Telegram text and as a Markdown document.
- `/gemini` runs Gemini CLI with `--skip-trust --yolo --prompt`.
- `/codex` runs Codex CLI with `exec --dangerously-bypass-approvals-and-sandbox`.

## Requirements

On the target machine:

- Linux with `systemd`.
- `apt`, `dnf`, or `yum`.
- Telegram bot token from `@BotFather`.
- Numeric Telegram user ID from `@userinfobot` or similar.

The installer can prepare Python, SQLite, rsync, Git, Node.js/npm, Codex CLI,
Gemini CLI, service directories, fallback `codex-plan`/`codex-run` wrappers and
the systemd unit on a clean system.

`codex-plan` and `codex-run` are expected to:

- accept the task text as one command argument;
- use `PROJECT_DIR` from env;
- use `OUT_DIR` from env;
- write final result as `plan-YYYYMMDD-HHMMSS.md` or `run-YYYYMMDD-HHMMSS.md`;
- print the result file path to stdout.

## Install

Clone or unpack the project, then run:

```bash
sudo ./install.sh
```

The installer asks for:

- Linux service user, and whether to create it;
- Telegram bot token;
- allowed Telegram user ID or comma-separated user IDs;
- workspace/project directory, and whether to create it;
- whether to install Node.js/npm;
- whether to install Codex CLI through npm;
- whether to install Gemini CLI through npm;
- whether to create fallback `codex-plan`/`codex-run` wrappers;
- install/data/env/systemd paths;
- whether to enable and start the service.

Then it creates:

```text
/opt/codex-telegram-bot/
/var/lib/codex-telegram-bot/
/etc/codex-telegram-bot.env
/etc/systemd/system/codex-telegram-bot.service
```

## Non-interactive install

```bash
sudo TELEGRAM_BOT_TOKEN='123456:ABC' \
  TELEGRAM_ALLOWED_USERS='123456789' \
  TARGET_USER='codexbot' \
  CREATE_TARGET_USER='yes' \
  PROJECT_DIR='/srv/projects/my-project' \
  CREATE_PROJECT_DIR='yes' \
  INSTALL_NODEJS='yes' \
  INSTALL_CODEX_CLI='yes' \
  INSTALL_GEMINI_CLI='yes' \
  CREATE_CODEX_WRAPPERS='yes' \
  ./install.sh
```

After install, authenticate the CLI tools under the service user:

```bash
sudo -iu codexbot codex login
sudo -iu codexbot gemini
sudo systemctl restart codex-telegram-bot
```

## Telegram commands

```text
/start
/help
/plan <task>
/run <approved task>
/gemini <task>
/codex <task>
/status
/jobs
/last
/cancel
```

Plain text without a command is treated as `/plan`.

Example:

```text
/plan Analyze GitLab issue #123 through GitLab MCP. Do not change files. Return implementation plan, risks and tests.
```

After review:

```text
/run Implement the approved plan for issue #123. Create a branch and commit. Do not push and do not create MR.
```

Direct YOLO-style execution:

```text
/gemini Analyze this repository, fix the failing test, and summarize the diff.
```

```text
/codex Analyze this repository, fix the failing test, and summarize the diff.
```

## Service management

```bash
sudo systemctl status codex-telegram-bot
sudo systemctl restart codex-telegram-bot
journalctl -u codex-telegram-bot -f
```

Edit config:

```bash
sudo nano /etc/codex-telegram-bot.env
sudo systemctl restart codex-telegram-bot
```

Inspect job database:

```bash
sqlite3 /var/lib/codex-telegram-bot/bot.sqlite3 \
  "select id, mode, status, created_at, started_at, finished_at, return_code, output_file from jobs order by id desc limit 20;"
```

## Publishing to GitHub

If GitHub CLI is installed:

```bash
git init
git add .
git commit -m "Initial codex telegram bot"
gh repo create f2re/codex-telegram-bot --private --source . --remote origin --push
```

Or public:

```bash
gh repo create f2re/codex-telegram-bot --public --source . --remote origin --push
```

## Security notes

Keep `TELEGRAM_ALLOWED_USERS` strict. Anyone in this allowlist can trigger local
AI CLI execution through your service.

The service avoids `shell=True`, runs only one job at a time, and defaults plain
text to `/plan`. Still, `/gemini` and `/codex` deliberately run in YOLO-style
approval bypass modes. They may modify files, run tools, create commits, and
access credentials available to the service user.

Recommended separation:

```text
/plan   → read-only analysis through your wrapper
/run    → implementation through your wrapper after explicit approval
/gemini → direct Gemini CLI YOLO execution
/codex  → direct Codex CLI approval/sandbox bypass execution
```

Do not run this bot as `root` unless you intentionally want Codex to have root-level access.
