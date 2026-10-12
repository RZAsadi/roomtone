#!/usr/bin/env bash
set -Eeuo pipefail

# RoomTone lifecycle dashboard. Supported on Ubuntu 22.04/24.04.
# Interactive: sudo bash install.sh. Automation: sudo bash install.sh install [domain],
# sudo bash install.sh update, or sudo bash install.sh uninstall.

APP_NAME=roomtone
APP_DIR=/opt/roomtone
SERVICE_FILE=/etc/systemd/system/roomtone.service
TURN_ENV=/etc/roomtone/turn.env
REPO_URL="${ROOMTONE_REPO:-https://github.com/RZAsadi/roomtone.git}"
BACKUP_ROOT=/var/backups/roomtone
ACTION="${1:-}"
DOMAIN="${2:-}"
TMP_DIR=""
BACKUP_DIR=""
VENV_STAGE=""
VENV_PREVIOUS=""
STAGE_REQUIREMENTS=""
VENV_NEW_STARTED=0
TRANSACTION_ACTIVE=0
UNINSTALL_ACTIVE=0
UNINSTALL_SERVICE_STOPPED=0
ROOMTONE_WAS_ENABLED=0
COTURN_WAS_ENABLED=0
CADDY_WAS_ENABLED=0
ROOMTONE_WAS_ACTIVE=0
COTURN_WAS_ACTIVE=0
CADDY_WAS_ACTIVE=0
SERVICE_STATE_CAPTURED=0

log() { printf '\n[RoomTone] %s\n' "$*"; }
die() { printf '[RoomTone] ERROR: %s\n' "$*" >&2; exit 1; }
restore_service_state() {
  systemctl daemon-reload 2>/dev/null || true
  if [[ "$ROOMTONE_WAS_ENABLED" == 1 ]]; then systemctl enable roomtone 2>/dev/null || true; else systemctl disable roomtone 2>/dev/null || true; fi
  if [[ "$COTURN_WAS_ENABLED" == 1 ]]; then systemctl enable coturn 2>/dev/null || true; else systemctl disable coturn 2>/dev/null || true; fi
  if [[ "$CADDY_WAS_ENABLED" == 1 ]]; then systemctl enable caddy 2>/dev/null || true; else systemctl disable caddy 2>/dev/null || true; fi
  if [[ -f "$SERVICE_FILE" && "$ROOMTONE_WAS_ACTIVE" == 1 ]]; then systemctl restart roomtone 2>/dev/null || true; else systemctl stop roomtone 2>/dev/null || true; fi
  if [[ -f /etc/turnserver.conf && "$COTURN_WAS_ACTIVE" == 1 ]]; then systemctl restart coturn 2>/dev/null || true; else systemctl stop coturn 2>/dev/null || true; fi
  if [[ -f /etc/caddy/Caddyfile ]] && caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    if [[ "$CADDY_WAS_ACTIVE" == 1 ]]; then systemctl reload-or-restart caddy 2>/dev/null || true; else systemctl stop caddy 2>/dev/null || true; fi
  elif [[ "$CADDY_WAS_ACTIVE" != 1 ]]; then systemctl stop caddy 2>/dev/null || true
  else log "WARNING: Caddy's restored config did not validate; leaving its previously active process untouched."
  fi
  SERVICE_STATE_CAPTURED=0
  UNINSTALL_SERVICE_STOPPED=0
}
restore_transaction() {
  [[ -z "$BACKUP_DIR" ]] && return 0
  if [[ "$UNINSTALL_ACTIVE" == 1 && -d "$BACKUP_DIR/opt/roomtone" ]]; then
    rm -rf -- "$APP_DIR"
    cp -a "$BACKUP_DIR/opt/roomtone" "$APP_DIR"
  elif [[ "$UNINSTALL_ACTIVE" != 1 && ! -d "$BACKUP_DIR/opt/roomtone" ]]; then
    # A first install had no application directory in its pre-change backup.
    # Remove the whole newly-created tree, including any database or other
    # artifacts created before the health check failed.
    rm -rf -- "$APP_DIR"
  fi
  local -a restore_pairs=(
    "$BACKUP_DIR/opt/roomtone/app.py|$APP_DIR/app.py"
    "$BACKUP_DIR/opt/roomtone/requirements.txt|$APP_DIR/requirements.txt"
    "$BACKUP_DIR/etc/roomtone/turn.env|$TURN_ENV"
    "$BACKUP_DIR/etc/turnserver.conf|/etc/turnserver.conf"
    "$BACKUP_DIR/etc/default/coturn|/etc/default/coturn"
    "$BACKUP_DIR/etc/systemd/system/roomtone.service|$SERVICE_FILE"
    "$BACKUP_DIR/etc/caddy/Caddyfile|/etc/caddy/Caddyfile"
  )
  for pair in "${restore_pairs[@]}"; do
    local source="${pair%%|*}" target="${pair#*|}"
    if [[ -e "$source" ]]; then
      install -d "$(dirname "$target")"
      cp -a "$source" "$target"
    else
      rm -f -- "$target"
    fi
  done
  if [[ -e "$BACKUP_DIR/etc/roomtone" ]]; then install -d -m 0750 -o root -g "$APP_NAME" /etc/roomtone; else rmdir /etc/roomtone 2>/dev/null || true; fi
  restore_service_state
  TRANSACTION_ACTIVE=0
  UNINSTALL_ACTIVE=0
  UNINSTALL_SERVICE_STOPPED=0
}

cleanup() {
  local status=$?
  trap - EXIT
  [[ -z "$STAGE_REQUIREMENTS" ]] || rm -f -- "$STAGE_REQUIREMENTS"
  if [[ -n "$VENV_PREVIOUS" && -d "$VENV_PREVIOUS" ]]; then
    rm -rf -- "$APP_DIR/venv"
    mv "$VENV_PREVIOUS" "$APP_DIR/venv"
    VENV_PREVIOUS=""
  elif [[ "$status" -ne 0 && "$TRANSACTION_ACTIVE" == 1 && "$VENV_NEW_STARTED" == 1 ]]; then
    rm -rf -- "$APP_DIR/venv"
  fi
  if [[ "$status" -ne 0 && "$TRANSACTION_ACTIVE" == 1 ]]; then restore_transaction; fi
  if [[ "$status" -ne 0 && "$TRANSACTION_ACTIVE" == 0 && "$SERVICE_STATE_CAPTURED" == 1 ]]; then restore_service_state; fi
  if [[ "$status" -ne 0 && "$UNINSTALL_SERVICE_STOPPED" == 1 ]]; then
    systemctl start roomtone 2>/dev/null || true
  fi
  [[ -z "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"
  [[ -z "$VENV_STAGE" ]] || rm -rf -- "$VENV_STAGE"
  exit "$status"
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage:
  sudo bash install.sh                  Open the interactive dashboard
  sudo bash install.sh install [domain] Install RoomTone
  sudo bash install.sh update           Update an existing installation
  sudo bash install.sh uninstall        Remove RoomTone after confirmation
EOF
}

require_root() {
  [[ "$EUID" -eq 0 ]] || die "Run with sudo/root, for example: sudo bash install.sh"
  [[ -r /etc/os-release ]] || die "Cannot identify this Linux distribution."
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" =~ ^(22\.04|24\.04)$ ]] ||
    die "Supported platforms are Ubuntu 22.04 and 24.04; detected ${PRETTY_NAME:-unknown}."
}

new_backup() {
  install -d -m 0700 "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  BACKUP_DIR="$(mktemp -d "$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$-XXXXXX")"
  chmod 0700 "$BACKUP_DIR"
  for path in "$APP_DIR" /etc/roomtone /etc/turnserver.conf /etc/default/coturn "$SERVICE_FILE" /etc/caddy/Caddyfile; do
    [[ ! -e "$path" ]] || cp -a --parents "$path" "$BACKUP_DIR/"
  done
  # Refresh the copied database using SQLite's online backup API so an active
  # service's WAL state is included in a consistent snapshot.
  if [[ -f "$APP_DIR/roomtone.db" && -d "$BACKUP_DIR/opt/roomtone" ]]; then
    python3 - "$APP_DIR/roomtone.db" "$BACKUP_DIR/opt/roomtone/roomtone.db" <<'PYDB'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1], timeout=10)
try:
    with sqlite3.connect(sys.argv[2]) as target:
        source.backup(target)
finally:
    source.close()
PYDB
    chmod 0600 "$BACKUP_DIR/opt/roomtone/roomtone.db"
  fi
  log "Created root-only backup: $BACKUP_DIR"
}

validate_domain() {
  DOMAIN="${DOMAIN,,}"
  [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "Invalid domain name: ${DOMAIN:-empty}."
}

read_existing_domain() {
  [[ -s "$TURN_ENV" ]] || die "No existing TURN configuration found. Choose Install first."
  DOMAIN="$(sed -n 's/^ROOMTONE_TURN_DOMAIN=//p' "$TURN_ENV" | head -n1)"
  [[ -n "$DOMAIN" ]] || die "Could not read ROOMTONE_TURN_DOMAIN from $TURN_ENV."
  validate_domain
}


write_caddy_candidate() {
  local block="$TMP_DIR/roomtone-site" expected_legacy expected_unmarked
  cat > "$block" <<EOF
# BEGIN RoomTone managed site
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
# END RoomTone managed site
EOF
  if [[ ! -f /etc/caddy/Caddyfile ]]; then
    cp "$block" "$TMP_DIR/Caddyfile"
  elif grep -q '^# BEGIN RoomTone managed site$' /etc/caddy/Caddyfile; then
    [[ "$(grep -c '^# BEGIN RoomTone managed site$' /etc/caddy/Caddyfile)" == 1 && "$(grep -c '^# END RoomTone managed site$' /etc/caddy/Caddyfile)" == 1 ]] &&
      awk '/^# BEGIN RoomTone managed site$/ { begin=NR } /^# END RoomTone managed site$/ { end=NR } END { exit !(begin>0 && end>begin) }' /etc/caddy/Caddyfile ||
      die "Caddyfile has incomplete, repeated, or out-of-order RoomTone markers; repair it before updating."
    awk '/^# BEGIN RoomTone managed site$/ { skip=1; next } /^# END RoomTone managed site$/ { skip=0; next } !skip { print }' /etc/caddy/Caddyfile > "$TMP_DIR/Caddyfile.base"
    cat "$TMP_DIR/Caddyfile.base" > "$TMP_DIR/Caddyfile"
    printf '\n' >> "$TMP_DIR/Caddyfile"
    cat "$block" >> "$TMP_DIR/Caddyfile"
  elif grep -q '^# Managed by RoomTone installer$' /etc/caddy/Caddyfile; then
    expected_legacy="$(printf '# Managed by RoomTone installer\n%s {\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:8000\n}' "$DOMAIN")"
    [[ "$(cat /etc/caddy/Caddyfile)" == "$expected_legacy" ]] || die "The legacy managed Caddyfile has additional edits. Back it up and merge the RoomTone site block manually before updating."
    cp "$block" "$TMP_DIR/Caddyfile"
  else
    # Safely adopt the exact previous/manual single-site template. Do not append
    # a second block for the same domain, and do not rewrite customized files.
    expected_unmarked="$(printf '%s {\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:8000\n}' "$DOMAIN")"
    if [[ "$(cat /etc/caddy/Caddyfile)" == "$expected_unmarked" ]]; then
      cp "$block" "$TMP_DIR/Caddyfile"
    elif grep -Eq "^[[:space:]]*${DOMAIN//./\\.}[[:space:]]*\\{" /etc/caddy/Caddyfile; then
      die "Caddyfile already defines $DOMAIN outside the RoomTone managed block. The existing file was preserved; merge the RoomTone site into that block or back up and remove the duplicate before retrying."
    else
      cp /etc/caddy/Caddyfile "$TMP_DIR/Caddyfile"
      printf '\n' >> "$TMP_DIR/Caddyfile"
      cat "$block" >> "$TMP_DIR/Caddyfile"
    fi
  fi
  caddy validate --config "$TMP_DIR/Caddyfile"
}

install_or_update() {
  local mode="$1"
  if [[ "$mode" == update ]]; then
    [[ -d "$APP_DIR" && -f "$APP_DIR/app.py" ]] || die "RoomTone is not installed at $APP_DIR. Choose Install first."
    [[ -n "$DOMAIN" ]] || read_existing_domain
  elif [[ -z "$DOMAIN" ]]; then
    [[ -t 0 ]] || die "Install requires a domain in noninteractive mode: sudo bash install.sh install voice.example.com"
    read -r -p "Domain for RoomTone (e.g. voice.example.com): " DOMAIN
  fi
  validate_domain
  [[ "$REPO_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/ ]] || die "ROOMTONE_REPO must be an HTTPS Git URL."
  if [[ "$mode" == install && -f "$APP_DIR/app.py" ]]; then
    die "An installation already exists. Choose Update to preserve its configuration and database."
  fi
  log "$([[ "$mode" == update ]] && printf 'Update' || printf 'Install') will deploy to $APP_DIR and serve https://$DOMAIN."
  log "The operation backs up the app, database, and managed configuration before changing services."
  if [[ -t 0 ]]; then
    local confirm
    read -r -p "Continue? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { log "Cancelled."; return 0; }
  elif [[ "${ROOMTONE_CONFIRM:-}" != yes ]]; then
    die "Noninteractive install/update requires ROOMTONE_CONFIRM=yes."
  fi

  systemctl is-enabled --quiet roomtone 2>/dev/null && ROOMTONE_WAS_ENABLED=1 || true
  systemctl is-enabled --quiet coturn 2>/dev/null && COTURN_WAS_ENABLED=1 || true
  systemctl is-enabled --quiet caddy 2>/dev/null && CADDY_WAS_ENABLED=1 || true
  systemctl is-active --quiet roomtone 2>/dev/null && ROOMTONE_WAS_ACTIVE=1 || true
  systemctl is-active --quiet coturn 2>/dev/null && COTURN_WAS_ACTIVE=1 || true
  systemctl is-active --quiet caddy 2>/dev/null && CADDY_WAS_ACTIVE=1 || true
  SERVICE_STATE_CAPTURED=1

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl git python3 python3-venv python3-pip caddy coturn openssl
  if ! id "$APP_NAME" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_NAME"
  fi

  TMP_DIR="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$TMP_DIR/repo"
  [[ -f "$TMP_DIR/repo/app.py" && -f "$TMP_DIR/repo/requirements.txt" ]] || die "Repository must contain app.py and requirements.txt at its root."
  python3 -m py_compile "$TMP_DIR/repo/app.py"
  new_backup
  TRANSACTION_ACTIVE=1

  # Validate and merge the managed site block before touching live configuration.
  write_caddy_candidate

  install -d -o "$APP_NAME" -g "$APP_NAME" "$APP_DIR"
  # install -d may leave ownership unchanged when the directory already
  # exists (for example after a failed/root-run deployment). SQLite creates
  # roomtone.db in this directory, so explicitly repair directory ownership.
  chown "$APP_NAME:$APP_NAME" "$APP_DIR"
  # An existing database can retain root-only ownership even after the parent
  # directory is repaired. Preserve its contents and mode, but let the service
  # account open and update it. Never follow a database symlink here.
  if [[ -f "$APP_DIR/roomtone.db" && ! -L "$APP_DIR/roomtone.db" ]]; then
    chown "$APP_NAME:$APP_NAME" "$APP_DIR/roomtone.db"
  fi
  for sqlite_sidecar in "$APP_DIR/roomtone.db-wal" "$APP_DIR/roomtone.db-shm"; do
    if [[ -f "$sqlite_sidecar" && ! -L "$sqlite_sidecar" ]]; then
      chown "$APP_NAME:$APP_NAME" "$sqlite_sidecar"
    fi
  done
  install -o "$APP_NAME" -g "$APP_NAME" "$TMP_DIR/repo/app.py" "$TMP_DIR/stage-app.py"
  # TMP_DIR is created by root with mode 0700, so the service account cannot
  # traverse it even if a staged file inside is owned by that account. Keep
  # requirements in the app-owned directory while pip runs as RoomTone.
  STAGE_REQUIREMENTS="$APP_DIR/.stage-requirements-$$.txt"
  install -o "$APP_NAME" -g "$APP_NAME" "$TMP_DIR/repo/requirements.txt" "$STAGE_REQUIREMENTS"
  VENV_STAGE="$APP_DIR/.venv-stage-$$"
  VENV_PREVIOUS="$APP_DIR/.venv-previous-$$"
  rm -rf -- "$VENV_STAGE" "$VENV_PREVIOUS"
  runuser -u "$APP_NAME" -- python3 -m venv "$VENV_STAGE"
  runuser -u "$APP_NAME" -- "$VENV_STAGE/bin/pip" install -r "$STAGE_REQUIREMENTS"
  # A successful pip exit is not sufficient if an incomplete/broken wheel was
  # installed. Catch import failures before changing the live environment.
  runuser -u "$APP_NAME" -- "$VENV_STAGE/bin/python" -c 'import uvicorn, uvicorn.config, uvicorn.importer; print("Validated staged Uvicorn", uvicorn.__version__)'

  install -d -m 0750 -o root -g "$APP_NAME" /etc/roomtone
  if [[ ! -s "$TURN_ENV" ]]; then
    local turn_secret
    turn_secret="$(openssl rand -hex 32)"
    printf 'ROOMTONE_TURN_DOMAIN=%s\nROOMTONE_TURN_SECRET=%s\n' "$DOMAIN" "$turn_secret" > "$TMP_DIR/turn.env"
    install -m 0640 -o root -g "$APP_NAME" "$TMP_DIR/turn.env" "$TURN_ENV"
  else
    if grep -q '^ROOMTONE_TURN_DOMAIN=' "$TURN_ENV"; then
      sed -i "s/^ROOMTONE_TURN_DOMAIN=.*/ROOMTONE_TURN_DOMAIN=$DOMAIN/" "$TURN_ENV"
    else
      printf 'ROOMTONE_TURN_DOMAIN=%s\n' "$DOMAIN" >> "$TURN_ENV"
    fi
    chown root:"$APP_NAME" "$TURN_ENV"
    chmod 0640 "$TURN_ENV"
  fi
  local turn_secret
  turn_secret="$(sed -n 's/^ROOMTONE_TURN_SECRET=//p' "$TURN_ENV" | head -n1)"
  [[ -n "$turn_secret" ]] || die "TURN secret is missing from $TURN_ENV; backup is at $BACKUP_DIR."
  cat > "$TMP_DIR/turnserver.conf" <<EOF
# Managed by RoomTone installer
listening-port=3478
fingerprint
use-auth-secret
static-auth-secret=$turn_secret
realm=$DOMAIN
total-quota=100
stale-nonce=600
no-loopback-peers
no-multicast-peers
min-port=49160
max-port=49200
listening-ip=0.0.0.0
EOF
  chmod 0600 "$TMP_DIR/turnserver.conf"
  # Keep distribution or administrator-specific coturn defaults intact.
  # Replace only the switch this installer needs, or append it if absent.
  if [[ -f /etc/default/coturn ]]; then
    if grep -q '^TURNSERVER_ENABLED=' /etc/default/coturn; then
      sed -i 's/^TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
    else
      printf '\nTURNSERVER_ENABLED=1\n' >> /etc/default/coturn
    fi
  else
    printf 'TURNSERVER_ENABLED=1\n' > /etc/default/coturn
  fi

  cat > "$TMP_DIR/roomtone.service" <<EOF
[Unit]
Description=RoomTone voice chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_NAME
Group=$APP_NAME
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$TURN_ENV
ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
EOF
  if [[ -d "$APP_DIR/venv" ]]; then mv "$APP_DIR/venv" "$VENV_PREVIOUS"; fi
  # Python venv scripts embed their creation path in shebangs, so validate a
  # staged environment above, then recreate/install at the final stable path.
  VENV_NEW_STARTED=1
  runuser -u "$APP_NAME" -- python3 -m venv "$APP_DIR/venv"
  runuser -u "$APP_NAME" -- "$APP_DIR/venv/bin/pip" install -r "$STAGE_REQUIREMENTS"
  runuser -u "$APP_NAME" -- "$APP_DIR/venv/bin/python" -c 'import uvicorn, uvicorn.config, uvicorn.importer; print("Validated production Uvicorn", uvicorn.__version__)'
  install -o "$APP_NAME" -g "$APP_NAME" "$TMP_DIR/repo/app.py" "$APP_DIR/app.py"
  install -o "$APP_NAME" -g "$APP_NAME" "$TMP_DIR/repo/requirements.txt" "$APP_DIR/requirements.txt"
  install -m 0600 "$TMP_DIR/turnserver.conf" /etc/turnserver.conf
  install -m 0644 "$TMP_DIR/roomtone.service" "$SERVICE_FILE"
  install -m 0644 "$TMP_DIR/Caddyfile" /etc/caddy/Caddyfile

  if ! systemctl daemon-reload || ! systemctl enable roomtone coturn caddy ||
     ! systemctl restart coturn || ! systemctl restart roomtone ||
     ! caddy validate --config /etc/caddy/Caddyfile || ! systemctl reload-or-restart caddy; then
    log "Deployment failed; rollback will restore the backed-up files and original service states. Backup: $BACKUP_DIR"
    return 1
  fi
  # Type=simple services are considered started before Uvicorn has necessarily
  # bound its socket. Poll readiness instead of racing the first listen() call.
  local health_attempt healthy=0
  for health_attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8000/api/rooms >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done
  if [[ "$healthy" != 1 ]]; then
    log "RoomTone did not become healthy within 30 seconds. Recent service diagnostics follow:"
    systemctl --no-pager --full status roomtone || true
    journalctl -u roomtone -n 80 --no-pager -o cat || true
    log "Deployment failed; rollback will restore the backed-up files and original service states. Backup: $BACKUP_DIR"
    return 1
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 3478/tcp; ufw allow 3478/udp; ufw allow 49160:49200/udp
  fi
  # Keep the old environment available until every failure-prone deployment
  # step has succeeded; cleanup can then restore it if a firewall command fails.
  [[ ! -d "$VENV_PREVIOUS" ]] || rm -rf -- "$VENV_PREVIOUS"
  VENV_PREVIOUS=""
  VENV_NEW_STARTED=0
  TRANSACTION_ACTIVE=0
  SERVICE_STATE_CAPTURED=0
  log "RoomTone $mode completed. URL: https://$DOMAIN"
  log "Backup: $BACKUP_DIR"
  log "Service: systemctl status roomtone; logs: journalctl -u roomtone -f"
}

uninstall_roomtone() {
  [[ -f "$SERVICE_FILE" || -d "$APP_DIR" ]] || die "No RoomTone installation was found."
  if [[ -t 0 ]]; then
    printf 'This will stop RoomTone and remove %s and its app/database files. A protected backup will be created first.\n' "$APP_DIR"
    local confirm
    read -r -p 'Type UNINSTALL ROOMTONE to continue: ' confirm
    [[ "$confirm" == 'UNINSTALL ROOMTONE' ]] || { log "Cancelled."; return 0; }
  elif [[ "${ROOMTONE_CONFIRM:-}" != 'UNINSTALL ROOMTONE' ]]; then
    die "Noninteractive uninstall requires ROOMTONE_CONFIRM='UNINSTALL ROOMTONE'."
  fi
  new_backup
  TMP_DIR="$(mktemp -d)"
  systemctl is-enabled --quiet roomtone 2>/dev/null && ROOMTONE_WAS_ENABLED=1 || true
  systemctl is-enabled --quiet coturn 2>/dev/null && COTURN_WAS_ENABLED=1 || true
  systemctl is-enabled --quiet caddy 2>/dev/null && CADDY_WAS_ENABLED=1 || true
  systemctl is-active --quiet roomtone 2>/dev/null && ROOMTONE_WAS_ACTIVE=1 || true
  systemctl is-active --quiet coturn 2>/dev/null && COTURN_WAS_ACTIVE=1 || true
  systemctl is-active --quiet caddy 2>/dev/null && CADDY_WAS_ACTIVE=1 || true
  SERVICE_STATE_CAPTURED=1
  if systemctl is-active --quiet roomtone; then
    UNINSTALL_SERVICE_STOPPED=1
    systemctl stop roomtone
  fi
  # Take the final database snapshot after stopping writers so the retained
  # uninstall backup includes every committed transaction.
  if [[ -f "$APP_DIR/roomtone.db" && -d "$BACKUP_DIR/opt/roomtone" ]]; then
    python3 - "$APP_DIR/roomtone.db" "$BACKUP_DIR/opt/roomtone/roomtone.db" <<'PYDB'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1], timeout=10)
try:
    with sqlite3.connect(sys.argv[2]) as target:
        source.backup(target)
finally:
    source.close()
PYDB
    chmod 0600 "$BACKUP_DIR/opt/roomtone/roomtone.db"
  fi
  TRANSACTION_ACTIVE=1
  UNINSTALL_ACTIVE=1
  systemctl disable roomtone 2>/dev/null || true
  rm -f -- "$SERVICE_FILE"
  local current_domain="" current_secret="" expected_turn="" expected_legacy_caddy=""
  if [[ -s "$TURN_ENV" ]]; then
    current_domain="$(sed -n 's/^ROOMTONE_TURN_DOMAIN=//p' "$TURN_ENV" | head -n1)"
    current_secret="$(sed -n 's/^ROOMTONE_TURN_SECRET=//p' "$TURN_ENV" | head -n1)"
  fi
  if [[ -n "$current_domain" && -n "$current_secret" && -f /etc/turnserver.conf ]] && grep -q '^# Managed by RoomTone installer$' /etc/turnserver.conf; then
    printf '# Managed by RoomTone installer\nlistening-port=3478\nfingerprint\nuse-auth-secret\nstatic-auth-secret=%s\nrealm=%s\ntotal-quota=100\nstale-nonce=600\nno-loopback-peers\nno-multicast-peers\nmin-port=49160\nmax-port=49200\nlistening-ip=0.0.0.0' "$current_secret" "$current_domain" > "$TMP_DIR/expected-turnserver.conf"
    if [[ "$(cat /etc/turnserver.conf)" == "$(cat "$TMP_DIR/expected-turnserver.conf")" ]]; then
      rm -f /etc/turnserver.conf
      systemctl disable --now coturn 2>/dev/null || true
    else
      log "Preserving edited /etc/turnserver.conf; review it manually."
    fi
  fi
  if [[ -f /etc/caddy/Caddyfile ]] && grep -q '^# BEGIN RoomTone managed site$' /etc/caddy/Caddyfile; then
    if [[ "$(grep -c '^# BEGIN RoomTone managed site$' /etc/caddy/Caddyfile)" == 1 && "$(grep -c '^# END RoomTone managed site$' /etc/caddy/Caddyfile)" == 1 ]] &&
       awk '/^# BEGIN RoomTone managed site$/ { begin=NR } /^# END RoomTone managed site$/ { end=NR } END { exit !(begin>0 && end>begin) }' /etc/caddy/Caddyfile; then
      awk '/^# BEGIN RoomTone managed site$/ { skip=1; next } /^# END RoomTone managed site$/ { skip=0; next } !skip { print }' /etc/caddy/Caddyfile > "$TMP_DIR/Caddyfile.remaining"
      if [[ -z "$(tr -d '[:space:]' < "$TMP_DIR/Caddyfile.remaining")" ]]; then rm -f /etc/caddy/Caddyfile; else install -m 0644 "$TMP_DIR/Caddyfile.remaining" /etc/caddy/Caddyfile; fi
    else
      log "Preserving Caddyfile because its RoomTone markers are incomplete or repeated."
    fi
  elif [[ -f /etc/caddy/Caddyfile ]] && grep -q '^# Managed by RoomTone installer$' /etc/caddy/Caddyfile; then
    expected_legacy_caddy="$(printf '# Managed by RoomTone installer\n%s {\n    encode zstd gzip\n    reverse_proxy 127.0.0.1:8000\n}' "$current_domain")"
    if [[ "$(cat /etc/caddy/Caddyfile)" == "$expected_legacy_caddy" ]]; then rm -f /etc/caddy/Caddyfile; else log "Preserving edited legacy Caddyfile; review its RoomTone site manually."; fi
  fi
  rm -rf -- "$APP_DIR"
  if [[ -s "$TURN_ENV" && -n "$current_domain" && -n "$current_secret" ]]; then
    printf 'ROOMTONE_TURN_DOMAIN=%s\nROOMTONE_TURN_SECRET=%s\n' "$current_domain" "$current_secret" > "$TMP_DIR/expected-turn.env"
    if cmp -s "$TURN_ENV" "$TMP_DIR/expected-turn.env"; then
      rm -f -- "$TURN_ENV"
    else
      log "Preserving $TURN_ENV because it contains additional or edited settings; remove it manually only after reviewing its contents."
    fi
  elif [[ -e "$TURN_ENV" ]]; then
    log "Preserving $TURN_ENV because its ownership/content cannot be safely identified; review it manually."
  fi
  rmdir /etc/roomtone 2>/dev/null || true
  systemctl daemon-reload
  if [[ ! -e /etc/caddy/Caddyfile ]]; then systemctl disable --now caddy 2>/dev/null || true; else caddy validate --config /etc/caddy/Caddyfile; systemctl reload-or-restart caddy; fi
  TRANSACTION_ACTIVE=0
  UNINSTALL_ACTIVE=0
  UNINSTALL_SERVICE_STOPPED=0
  SERVICE_STATE_CAPTURED=0
  log "RoomTone application removed. Protected backup (including database/configuration): $BACKUP_DIR"
  log "Shared packages, firewall rules, and unmarked legacy proxy/TURN files were left in place. Review the backup before deleting it."
}

dashboard() {
  [[ -t 0 ]] || { usage >&2; die "No interactive terminal. Pass install [domain], update, or uninstall explicitly."; }
  while true; do
    printf '\nRoomTone dashboard\n  1) Install\n  2) Update\n  3) Uninstall\n  4) Exit\n'
    local choice
    read -r -p 'Choose an option [1-4]: ' choice || return 0
    case "$choice" in
      1)
        DOMAIN=""
        if ! read -r -p 'Domain for RoomTone: ' DOMAIN; then log 'Cancelled.'; continue; fi
        if [[ -z "$DOMAIN" ]]; then log 'Cancelled: no domain entered.'; continue; fi
        install_or_update install
        ;;
      2) DOMAIN=""; install_or_update update ;;
      3) uninstall_roomtone ;;
      4) log 'Goodbye.'; return 0 ;;
      *) log 'Invalid selection. Choose 1, 2, 3, or 4.' ;;
    esac
  done
}

# Help is informational and should remain available without root or a supported
# deployment host. Lifecycle actions still pass through the platform/root guard.
case "$ACTION" in
  -h|--help|help) usage; exit 0 ;;
esac

require_root
case "$ACTION" in
  "") dashboard ;;
  install) install_or_update install ;;
  update) install_or_update update ;;
  uninstall) uninstall_roomtone ;;
  *) DOMAIN="$ACTION"; install_or_update install ;;
esac
