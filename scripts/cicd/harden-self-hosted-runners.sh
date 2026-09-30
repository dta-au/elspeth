#!/usr/bin/env bash
# Audit or harden the four project-owned Nyx GitHub Actions runners.
#
# The 2026-10-01 custody review found runner credentials and environment files
# at mode 0664 and services without a restrictive umask or systemd sandboxing.
# --check is read-only and is the default. --execute requires root, applies the
# exact checked policy, restarts each runner sequentially and then re-audits.

set -euo pipefail

MODE=check
RUNNER_ROOT=/opt/actions-runner
SERVICE_DIR=/etc/systemd/system
OWNER=actions-runner
GROUP=actions-runner
EXPECTED_COUNT=4
SERVICE_PREFIX=actions.runner.dta-au-elspeth.nyx-elspeth

usage() {
  cat <<'EOF'
Usage: scripts/cicd/harden-self-hosted-runners.sh [--check|--execute]
       [--runner-root DIR] [--service-dir DIR] [--owner USER] [--group GROUP]

--check is read-only and is the default. --execute is allowed only for the
live /opt/actions-runner and /etc/systemd/system paths and requires root.
EOF
}

while (($#)); do
  case "$1" in
    --check)
      MODE=check
      shift
      ;;
    --execute)
      MODE=execute
      shift
      ;;
    --runner-root)
      RUNNER_ROOT=$2
      shift 2
      ;;
    --service-dir)
      SERVICE_DIR=$2
      shift 2
      ;;
    --owner)
      OWNER=$2
      shift 2
      ;;
    --group)
      GROUP=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

resolve_uid() {
  if [[ $OWNER =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$OWNER"
  else
    id -u "$OWNER"
  fi
}

resolve_gid() {
  if [[ $GROUP =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$GROUP"
  else
    getent group "$GROUP" | cut -d: -f3
  fi
}

hardening_lines=(
  '[Service]'
  'UMask=0077'
  'NoNewPrivileges=true'
  'PrivateTmp=true'
  'ProtectSystem=full'
  'ProtectClock=true'
  'ProtectControlGroups=true'
  'ProtectHostname=true'
  'ProtectKernelLogs=true'
  'ProtectKernelModules=true'
  'ProtectKernelTunables=true'
  'LockPersonality=true'
  'RestrictRealtime=true'
  'RestrictSUIDSGID=true'
  'CapabilityBoundingSet='
  'AmbientCapabilities='
)

write_drop_in() {
  local destination=$1
  install -d -m 0755 -o root -g root "$(dirname "$destination")"
  {
    printf '%s\n' "${hardening_lines[@]}"
  } >"$destination"
  chown root:root "$destination"
  chmod 0644 "$destination"
}

apply_hardening() {
  if [[ $RUNNER_ROOT != /opt/actions-runner || $SERVICE_DIR != /etc/systemd/system ]]; then
    echo "--execute is restricted to the live runner and systemd paths" >&2
    exit 2
  fi
  if ((EUID != 0)); then
    echo "--execute requires root; run with sudo" >&2
    exit 2
  fi

  local number runner unit drop_in file
  for number in $(seq 1 "$EXPECTED_COUNT"); do
    runner="$RUNNER_ROOT/elspeth-nyx-$number"
    unit="$SERVICE_DIR/$SERVICE_PREFIX-$number.service"
    [[ -d $runner && -f $unit ]] || {
      echo "refusing partial mutation: missing runner $number or its service" >&2
      exit 1
    }
    for file in .credentials .credentials_rsaparams .env .path; do
      [[ -f $runner/$file ]] || {
        echo "refusing partial mutation: missing $runner/$file" >&2
        exit 1
      }
    done
  done

  for number in $(seq 1 "$EXPECTED_COUNT"); do
    runner="$RUNNER_ROOT/elspeth-nyx-$number"
    unit="$SERVICE_DIR/$SERVICE_PREFIX-$number.service"
    drop_in="$SERVICE_DIR/$SERVICE_PREFIX-$number.service.d/10-elspeth-hardening.conf"
    chown "$OWNER:$GROUP" "$runner"
    chmod 0700 "$runner"
    for file in .credentials .credentials_rsaparams .env .path; do
      chown "$OWNER:$GROUP" "$runner/$file"
      chmod 0600 "$runner/$file"
    done
    chmod 0644 "$unit"
    write_drop_in "$drop_in"
  done

  systemctl daemon-reload
  for number in $(seq 1 "$EXPECTED_COUNT"); do
    unit="$SERVICE_PREFIX-$number.service"
    systemctl restart "$unit"
    systemctl is-active --quiet "$unit"
  done
}

audit_hardening() {
  local expected_uid expected_gid
  expected_uid=$(resolve_uid)
  expected_gid=$(resolve_gid)

  local failures=0
  local -a runners
  mapfile -t runners < <(find "$RUNNER_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'elspeth-nyx-*' -print | sort)
  printf 'runner_count=%s\n' "${#runners[@]}"
  if ((${#runners[@]} != EXPECTED_COUNT)); then
    echo "[FAIL] expected $EXPECTED_COUNT runner directories"
    failures=$((failures + 1))
  fi

  local number runner unit drop_in file actual_mode actual_uid actual_gid line
  for number in $(seq 1 "$EXPECTED_COUNT"); do
    runner="$RUNNER_ROOT/elspeth-nyx-$number"
    unit="$SERVICE_DIR/$SERVICE_PREFIX-$number.service"
    drop_in="$SERVICE_DIR/$SERVICE_PREFIX-$number.service.d/10-elspeth-hardening.conf"

    if [[ ! -d $runner ]]; then
      echo "[FAIL] missing runner directory $runner"
      failures=$((failures + 1))
      continue
    fi
    if [[ ! -x $runner ]]; then
      echo "[ERROR] cannot traverse $runner; rerun --check as root" >&2
      return 2
    fi
    read -r actual_mode actual_uid actual_gid < <(stat -c '%a %u %g' "$runner")
    if [[ $actual_mode != 700 ]]; then
      echo "[FAIL] $runner mode $actual_mode, expected 700"
      failures=$((failures + 1))
    fi
    if [[ $actual_uid != "$expected_uid" || $actual_gid != "$expected_gid" ]]; then
      echo "[FAIL] $runner owner $actual_uid:$actual_gid, expected $expected_uid:$expected_gid"
      failures=$((failures + 1))
    fi

    for file in .credentials .credentials_rsaparams .env .path; do
      if [[ ! -f $runner/$file ]]; then
        echo "[FAIL] $runner missing $file"
        failures=$((failures + 1))
        continue
      fi
      read -r actual_mode actual_uid actual_gid < <(stat -c '%a %u %g' "$runner/$file")
      if [[ $actual_mode != 600 ]]; then
        echo "[FAIL] $runner/$file mode $actual_mode, expected 600"
        failures=$((failures + 1))
      fi
      if [[ $actual_uid != "$expected_uid" || $actual_gid != "$expected_gid" ]]; then
        echo "[FAIL] $runner/$file owner $actual_uid:$actual_gid, expected $expected_uid:$expected_gid"
        failures=$((failures + 1))
      fi
    done

    if [[ ! -f $unit ]]; then
      echo "[FAIL] missing service unit $unit"
      failures=$((failures + 1))
      continue
    fi
    actual_mode=$(stat -c '%a' "$unit")
    if [[ $actual_mode != 644 ]]; then
      echo "[FAIL] $unit mode $actual_mode, expected 644"
      failures=$((failures + 1))
    fi
    if ! grep -Fxq "User=$OWNER" "$unit"; then
      echo "[FAIL] $unit does not run as $OWNER"
      failures=$((failures + 1))
    fi
    if ! grep -Fxq "WorkingDirectory=$runner" "$unit"; then
      echo "[FAIL] $unit has the wrong working directory"
      failures=$((failures + 1))
    fi

    if [[ ! -f $drop_in ]]; then
      echo "[FAIL] missing hardening drop-in $drop_in"
      failures=$((failures + 1))
      continue
    fi
    for line in "${hardening_lines[@]}"; do
      if ! grep -Fxq "$line" "$drop_in"; then
        echo "[FAIL] $drop_in missing: $line"
        failures=$((failures + 1))
      fi
    done
  done

  if ((failures)); then
    printf 'runner_host_controls=FAIL failures=%s\n' "$failures"
    return 1
  fi
  echo 'runner_host_controls=PASS'
}

if [[ $MODE == execute ]]; then
  apply_hardening
fi
audit_hardening
