#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="codex-telegram-bot"
DEFAULT_INSTALL_DIR="/opt/${SERVICE_NAME}"
DEFAULT_DATA_DIR="/var/lib/${SERVICE_NAME}"
DEFAULT_ENV_FILE="/etc/${SERVICE_NAME}.env"
DEFAULT_SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_PROJECT_DIR="/srv/projects/telegram-ai-workspace"
DEFAULT_NODE_MAJOR="22"
DEFAULT_CODEX_NPM_PACKAGE="@openai/codex"
DEFAULT_GEMINI_NPM_PACKAGE="@google/gemini-cli"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "Run as root: sudo ./install.sh"
  fi
}

prompt() {
  local var_name="$1"
  local label="$2"
  local default_value="${3:-}"
  local value=""

  if [[ -n "${!var_name:-}" ]]; then
    printf -v "$var_name" '%s' "${!var_name}"
    return
  fi

  if [[ -n "$default_value" ]]; then
    read -r -p "$label [$default_value]: " value
    value="${value:-$default_value}"
  else
    while [[ -z "$value" ]]; do
      read -r -p "$label: " value
    done
  fi

  printf -v "$var_name" '%s' "$value"
}

prompt_secret() {
  local var_name="$1"
  local label="$2"
  local value=""

  if [[ -n "${!var_name:-}" ]]; then
    printf -v "$var_name" '%s' "${!var_name}"
    return
  fi

  while [[ -z "$value" ]]; do
    read -r -s -p "$label: " value
    printf '\n'
  done

  printf -v "$var_name" '%s' "$value"
}

prompt_yes_no() {
  local var_name="$1"
  local label="$2"
  local default_value="${3:-yes}"
  local value=""
  local hint="[y/N]"

  if [[ -n "${!var_name:-}" ]]; then
    case "${!var_name}" in
      y|Y|yes|YES|true|TRUE|1) printf -v "$var_name" 'yes' ;;
      n|N|no|NO|false|FALSE|0) printf -v "$var_name" 'no' ;;
      *) fail "Invalid boolean for $var_name: ${!var_name}" ;;
    esac
    return
  fi

  if [[ "$default_value" == "yes" ]]; then
    hint="[Y/n]"
  fi

  while true; do
    read -r -p "$label $hint: " value
    value="${value:-$default_value}"
    case "$value" in
      y|Y|yes|YES) printf -v "$var_name" 'yes'; return ;;
      n|N|no|NO) printf -v "$var_name" 'no'; return ;;
      *) warn "Answer yes or no." ;;
    esac
  done
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

user_exists() {
  id "$1" >/dev/null 2>&1
}

resolve_for_user() {
  local target_user="$1"
  local command_name="$2"

  if [[ "$command_name" == */* ]]; then
    [[ -x "$command_name" ]] && printf '%s\n' "$command_name" && return 0
    return 1
  fi

  runuser -u "$target_user" -- bash -lc "command -v -- $(printf '%q' "$command_name")" 2>/dev/null || return 1
}

escape_env_value() {
  # systemd EnvironmentFile accepts simple KEY=value lines. Keep values quoted.
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

install_os_packages() {
  info "Installing OS packages"
  if command_exists apt-get; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      bash ca-certificates curl git gnupg python3 python3-pip python3-venv rsync sqlite3
  elif command_exists dnf; then
    dnf install -y bash ca-certificates curl git gnupg2 python3 python3-pip rsync sqlite sqlite-devel
  elif command_exists yum; then
    yum install -y bash ca-certificates curl git gnupg2 python3 python3-pip rsync sqlite
  else
    fail "Unsupported package manager. Install bash, curl, git, python3, python3-venv, pip, rsync and sqlite3 manually."
  fi
}

install_nodejs_if_requested() {
  if [[ "$INSTALL_NODEJS" != "yes" ]]; then
    return
  fi

  if command_exists node && command_exists npm; then
    info "Node.js already installed: $(node --version), npm $(npm --version)"
    return
  fi

  info "Installing Node.js ${NODE_MAJOR}.x"
  if command_exists apt-get; then
    local setup_script="/tmp/nodesource_setup_${NODE_MAJOR}.x"
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$setup_script"
    bash "$setup_script"
    DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
    rm -f "$setup_script"
  elif command_exists dnf; then
    dnf module reset -y nodejs || true
    dnf module enable -y "nodejs:${NODE_MAJOR}" || true
    dnf install -y nodejs npm
  elif command_exists yum; then
    yum install -y nodejs npm
  else
    fail "Cannot install Node.js automatically on this system."
  fi
}

install_npm_cli_if_requested() {
  local target_user="$1"
  local package_name="$2"
  local command_name="$3"
  local install_flag="$4"

  if [[ "$install_flag" != "yes" ]]; then
    return
  fi

  command_exists npm || fail "npm is required to install $package_name"

  if resolve_for_user "$target_user" "$command_name" >/dev/null; then
    info "$command_name already available for $target_user: $(resolve_for_user "$target_user" "$command_name")"
    return
  fi

  info "Installing $package_name globally with npm"
  npm install -g "$package_name"
}

make_project_dir() {
  local target_user="$1"
  local target_group="$2"
  if [[ "$CREATE_PROJECT_DIR" == "yes" ]]; then
    install -d -o "$target_user" -g "$target_group" "$PROJECT_DIR"
  elif [[ ! -d "$PROJECT_DIR" ]]; then
    fail "PROJECT_DIR does not exist: $PROJECT_DIR"
  fi
}

write_codex_wrappers() {
  local install_dir="$1"
  local target_user="$2"
  local target_group="$3"
  local wrapper_dir="$install_dir/bin"

  install -d -o "$target_user" -g "$target_group" "$wrapper_dir"

  cat > "$wrapper_dir/codex-plan" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

task="${1:-}"
if [[ -z "$task" ]]; then
  printf 'Usage: codex-plan <task>\n' >&2
  exit 2
fi

project_dir="${PROJECT_DIR:?PROJECT_DIR is required}"
out_dir="${OUT_DIR:?OUT_DIR is required}"
mkdir -p "$out_dir"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out_file="$out_dir/plan-$stamp.md"

codex exec \
  --cd "$project_dir" \
  --skip-git-repo-check \
  --sandbox read-only \
  --ask-for-approval never \
  --output-last-message "$out_file" \
  "$task"

printf '%s\n' "$out_file"
EOF

  cat > "$wrapper_dir/codex-run" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

task="${1:-}"
if [[ -z "$task" ]]; then
  printf 'Usage: codex-run <task>\n' >&2
  exit 2
fi

project_dir="${PROJECT_DIR:?PROJECT_DIR is required}"
out_dir="${OUT_DIR:?OUT_DIR is required}"
mkdir -p "$out_dir"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out_file="$out_dir/run-$stamp.md"

codex exec \
  --cd "$project_dir" \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  --output-last-message "$out_file" \
  "$task"

printf '%s\n' "$out_file"
EOF

  chmod 0755 "$wrapper_dir/codex-plan" "$wrapper_dir/codex-run"
  chown "$target_user:$target_group" "$wrapper_dir/codex-plan" "$wrapper_dir/codex-run"

  CODEX_PLAN_CMD="$wrapper_dir/codex-plan"
  CODEX_RUN_CMD="$wrapper_dir/codex-run"
}

detect_command_or_default() {
  local target_user="$1"
  local command_name="$2"
  local default_value="$3"
  local detected=""
  detected="$(resolve_for_user "$target_user" "$command_name" || true)"
  printf '%s\n' "${detected:-$default_value}"
}

print_auth_guidance() {
  local target_user="$1"
  cat <<EOF

Authentication checks:
  Codex CLI must be logged in for the service user:
    sudo -iu $target_user codex login

  Gemini CLI must be logged in for the service user:
    sudo -iu $target_user gemini

  After authentication, restart and test:
    sudo systemctl restart $SERVICE_NAME
    journalctl -u $SERVICE_NAME -f

EOF
}

main() {
  need_root

  if [[ ! -f "${SCRIPT_DIR}/app.py" || ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    fail "install.sh must be run from the project directory containing app.py and requirements.txt"
  fi

  local default_user="${SUDO_USER:-codexbot}"
  if [[ "$default_user" == "root" ]]; then
    default_user="codexbot"
  fi

  prompt TARGET_USER "Linux user to run the bot" "$default_user"
  prompt_yes_no CREATE_TARGET_USER "Create user '$TARGET_USER' if missing" "yes"
  prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token"
  prompt TELEGRAM_ALLOWED_USERS "Allowed Telegram user IDs, comma-separated" ""
  prompt PROJECT_DIR "Workspace/project directory for AI CLIs" "$DEFAULT_PROJECT_DIR"
  prompt_yes_no CREATE_PROJECT_DIR "Create workspace directory if missing" "yes"
  prompt_yes_no INSTALL_NODEJS "Install Node.js/npm if missing" "yes"
  prompt NODE_MAJOR "Node.js major version for apt/dnf installs" "$DEFAULT_NODE_MAJOR"
  prompt_yes_no INSTALL_CODEX_CLI "Install Codex CLI with npm if missing" "yes"
  prompt CODEX_NPM_PACKAGE "Codex npm package" "$DEFAULT_CODEX_NPM_PACKAGE"
  prompt_yes_no INSTALL_GEMINI_CLI "Install Gemini CLI with npm if missing" "yes"
  prompt GEMINI_NPM_PACKAGE "Gemini npm package" "$DEFAULT_GEMINI_NPM_PACKAGE"
  prompt_yes_no CREATE_CODEX_WRAPPERS "Create codex-plan/codex-run fallback wrappers" "yes"
  prompt INSTALL_DIR "Install directory" "$DEFAULT_INSTALL_DIR"
  prompt DATA_DIR "Data directory" "$DEFAULT_DATA_DIR"
  prompt ENV_FILE "Environment file" "$DEFAULT_ENV_FILE"
  prompt SERVICE_FILE "systemd service file" "$DEFAULT_SERVICE_FILE"
  prompt_yes_no START_SERVICE "Enable and start systemd service now" "yes"

  install_os_packages
  install_nodejs_if_requested

  if ! user_exists "$TARGET_USER"; then
    if [[ "$CREATE_TARGET_USER" != "yes" ]]; then
      fail "User does not exist: $TARGET_USER"
    fi
    info "Creating service user: $TARGET_USER"
    useradd --create-home --shell /bin/bash "$TARGET_USER"
  fi

  local target_group
  target_group="$(id -gn "$TARGET_USER")"

  local target_home
  target_home="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  [[ -n "$target_home" ]] || fail "Cannot detect home directory for user: $TARGET_USER"

  install_npm_cli_if_requested "$TARGET_USER" "$CODEX_NPM_PACKAGE" codex "$INSTALL_CODEX_CLI"
  install_npm_cli_if_requested "$TARGET_USER" "$GEMINI_NPM_PACKAGE" gemini "$INSTALL_GEMINI_CLI"

  make_project_dir "$TARGET_USER" "$target_group"

  info "Install directory: $INSTALL_DIR"
  info "Data directory: $DATA_DIR"
  install -d -o "$TARGET_USER" -g "$target_group" "$INSTALL_DIR"
  install -d -o "$TARGET_USER" -g "$target_group" "$DATA_DIR" "$DATA_DIR/logs" "$DATA_DIR/results"

  info "Copying application files"
  rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    "$SCRIPT_DIR/" "$INSTALL_DIR/"

  chown -R "$TARGET_USER:$target_group" "$INSTALL_DIR" "$DATA_DIR"

  info "Creating Python virtual environment"
  runuser -u "$TARGET_USER" -- python3 -m venv "$INSTALL_DIR/venv"
  runuser -u "$TARGET_USER" -- "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
  runuser -u "$TARGET_USER" -- "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

  if [[ "$CREATE_CODEX_WRAPPERS" == "yes" ]]; then
    info "Creating codex-plan/codex-run fallback wrappers"
    write_codex_wrappers "$INSTALL_DIR" "$TARGET_USER" "$target_group"
  else
    prompt CODEX_PLAN_CMD "Path to codex-plan" "$(detect_command_or_default "$TARGET_USER" codex-plan "$target_home/bin/codex-plan")"
    prompt CODEX_RUN_CMD "Path to codex-run" "$(detect_command_or_default "$TARGET_USER" codex-run "$target_home/bin/codex-run")"
  fi

  prompt CODEX_CMD "Path to codex CLI" "$(detect_command_or_default "$TARGET_USER" codex codex)"
  prompt GEMINI_CMD "Path to gemini CLI" "$(detect_command_or_default "$TARGET_USER" gemini gemini)"

  local detected_codex_dir=""
  local detected_gemini_dir=""
  detected_codex_dir="$(dirname "$(resolve_for_user "$TARGET_USER" codex || echo /usr/local/bin)")"
  detected_gemini_dir="$(dirname "$(resolve_for_user "$TARGET_USER" gemini || echo /usr/local/bin)")"
  local service_path="$INSTALL_DIR/bin:$target_home/bin:$target_home/.local/bin:$detected_codex_dir:$detected_gemini_dir:/usr/local/bin:/usr/bin:/bin"

  resolve_for_user "$TARGET_USER" "$CODEX_CMD" >/dev/null || warn "codex CLI is not executable or not in PATH: $CODEX_CMD"
  resolve_for_user "$TARGET_USER" "$GEMINI_CMD" >/dev/null || warn "gemini CLI is not executable or not in PATH: $GEMINI_CMD"
  [[ -x "$CODEX_PLAN_CMD" ]] || warn "codex-plan is not executable or does not exist: $CODEX_PLAN_CMD"
  [[ -x "$CODEX_RUN_CMD" ]] || warn "codex-run is not executable or does not exist: $CODEX_RUN_CMD"

  info "Writing environment file: $ENV_FILE"
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$(escape_env_value "$TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USERS=$(escape_env_value "$TELEGRAM_ALLOWED_USERS")
PROJECT_DIR=$(escape_env_value "$PROJECT_DIR")
DATA_DIR=$(escape_env_value "$DATA_DIR")
CODEX_PLAN_CMD=$(escape_env_value "$CODEX_PLAN_CMD")
CODEX_RUN_CMD=$(escape_env_value "$CODEX_RUN_CMD")
CODEX_CMD=$(escape_env_value "$CODEX_CMD")
GEMINI_CMD=$(escape_env_value "$GEMINI_CMD")
HOME=$(escape_env_value "$target_home")
SERVICE_PATH=$(escape_env_value "$service_path")
TELEGRAM_POLL_TIMEOUT=50
HTTP_TIMEOUT=70
EOF
  chmod 0600 "$ENV_FILE"
  chown root:root "$ENV_FILE"

  info "Writing systemd unit: $SERVICE_FILE"
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram AI CLI Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Group=$target_group
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/app.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true

# HOME is intentionally not protected because Codex/Gemini normally use
# user-level auth, config, SSH keys and tools under the target account.

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  if [[ "$START_SERVICE" == "yes" ]]; then
    info "Enabling and starting service"
    systemctl enable --now "$SERVICE_NAME"
  else
    info "Service installed but not started"
  fi

  printf '\n'
  info "Installed."
  if [[ "$START_SERVICE" == "yes" ]]; then
    systemctl --no-pager --full status "$SERVICE_NAME" || true
  fi

  print_auth_guidance "$TARGET_USER"

  cat <<EOF
Telegram smoke tests:
  /start
  /plan Check repository structure. Do not change files.
  /gemini Check repository structure and return a concise summary.
  /codex Check repository structure and return a concise summary.

Config:
  $ENV_FILE

Logs:
  journalctl -u $SERVICE_NAME -f

EOF
}

main "$@"
