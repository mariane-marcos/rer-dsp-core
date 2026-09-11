#!/usr/bin/env bash
# Usage: ./setup.sh   then ./start.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

DSP_ORCHESTRATION_SCRIPT="setup.sh"
TOTAL_STEPS=10

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/common.sh"

if [ "$#" -gt 0 ]; then
  error "Run ./setup.sh with no arguments and follow the menu."
  exit 1
fi

step_header 1 "Prerequisites (Docker)"
require_not_root
require_docker

step_header 2 "Environment (.env)"
ensure_dotenv
reject_legacy_migration_env
ensure_dsp_repositories --backend --frontend

step_header 3 "Setup mode"
prompt_setup_data_mode

WILL_MIGRATE="${WILL_MIGRATE:-false}"
KEEP_MIGRATION_SERVICE="${KEEP_MIGRATION_SERVICE:-false}"

if [ "$SETUP_MODE" = "demo" ]; then
  info "Mode: demonstration (built-in seed)"
elif [ "$WILL_MIGRATE" = "true" ]; then
  info "Mode: real adopter — first migration runs during setup (${MIGRATION_EXECUTION_MODE:-once})"
else
  info "Mode: real adopter — first migration scheduled (${MIGRATION_EXECUTION_MODE})"
fi

MIGRATION_CONFIG_EXAMPLE="$ROOT_DIR/config/Job-Data-Migration/application/application.yaml.example"
MIGRATION_CONFIG="$ROOT_DIR/config/Job-Data-Migration/application/application.yaml"

if [ "$SETUP_MODE" = "demo" ]; then
  step_header 4 "Adopter configs (quickstart UI / map layers)"
  ensure_quickstart_adopter_configs

  step_header 5 "Confirmation"
  warn "Demonstration data only — no JDBC source and no migration job."
  if ! prompt_yes_no "Do you want to continue with this setup?"; then
    info "Setup cancelled."
    exit 0
  fi

  step_header 6 "Databases"
  start_databases_and_wait

  step_header 7 "Quickstart seed"
  apply_quickstart_seed

  step_header 8 "GeoServers (publish layers)"
  use_quickstart_layer_srids
  start_geoserver_exhibition "populate" ""
  start_geoserver_download "populate"

  ok "Setup finished — demonstration data is ready on Docker volumes."
  echo ""
  echo "Next: start the application stacks with:"
  echo "  ./start.sh"
  echo ""
  echo "Day-to-day: prefer ./start.sh (does not re-run seed)."
  echo "Real adopter data later: run ./config.sh and then ./setup.sh (Real adopter)."
  echo "Reset DBs (lose data): docker compose down -v && ./setup.sh"
  echo ""
  exit 0
fi

# --- Real adopter path ---

step_header 4 "Adopter configuration"
ensure_adopter_config

step_header 5 "Job repositories"
ensure_dsp_repositories --job --geo-file-job

step_header 6 "Map layers config (WMS / GeoServer)"

ensure_map_layers_config
ensure_download_themes_config

step_header 7 "Migration config (JDBC + ETL)"

if [ ! -f "$MIGRATION_CONFIG_EXAMPLE" ]; then
  error "Migration config template not found at: $MIGRATION_CONFIG_EXAMPLE"
  exit 1
fi

if [ ! -f "$MIGRATION_CONFIG" ]; then
  error "Migration configuration file not found:"
  echo "        $MIGRATION_CONFIG"
  error "Run ./config.sh to generate the active configuration from the adopter wizard."
  error "Or choose Demonstration in ./setup.sh."
  exit 1
fi

MIGRATION_CONFIG_READY=true
if cmp -s "$MIGRATION_CONFIG" "$MIGRATION_CONFIG_EXAMPLE"; then
  warn "Migration config is still identical to the generic template:"
  echo "       $MIGRATION_CONFIG"
  warn "Replace <placeholders> with your adopter source mapping before migrating."
  MIGRATION_CONFIG_READY=false
else
  ok "Migration config found: $MIGRATION_CONFIG"
fi

print_migration_preview "$MIGRATION_CONFIG" "$WILL_MIGRATE"

step_header 8 "Confirmation"

if [ "$MIGRATION_CONFIG_READY" = "false" ]; then
  error "Migration will run (now or on the schedule), but application.yaml is still a copy of the template."
  error "Edit $MIGRATION_CONFIG (or run ./config.sh) and run './setup.sh' again."
  error "Or choose Demonstration in ./setup.sh."
  exit 1
fi

if ! prompt_yes_no "Do you want to continue with this setup?"; then
  info "Setup cancelled."
  exit 0
fi

step_header 9 "Databases (+ migration)"

start_databases_and_wait

if [ "$WILL_MIGRATE" = "true" ]; then
  info "Running initial data migration (profile=migration)..."
  run_migration_job_once
  ok "Initial migration finished"
fi

persist_migration_env

if [ "$KEEP_MIGRATION_SERVICE" = "true" ]; then
  if [ "$WILL_MIGRATE" != "true" ]; then
    info "First load is scheduled — not running the job during this setup."
  fi
  start_migration_service_stack
  ok "Migration service stack is running (${MIGRATION_EXECUTION_MODE}${MIGRATION_CRON:+ cron=${MIGRATION_CRON}}${MIGRATION_SCHEDULED_AT:+ at=${MIGRATION_SCHEDULED_AT}})"
fi

step_header 10 "GeoServers (publish layers)"

if [ "$WILL_MIGRATE" = "true" ]; then
  start_geoserver_exhibition "populate" "$MIGRATION_CONFIG"
  start_geoserver_download "populate"
else
  info "Starting GeoServers now; layers are published after the first scheduled migration."
  start_geoserver_exhibition "start" "$MIGRATION_CONFIG"
  start_geoserver_download "start"
fi

if [ "$WILL_MIGRATE" = "true" ]; then
  ok "Setup finished — data is ready on Docker volumes."
else
  ok "Setup finished — databases stay empty until the scheduled first load."
fi
echo ""
echo "Next: start the application stacks with:"
echo "  ./start.sh"
echo ""
echo "Day-to-day: prefer ./start.sh (does not re-run migration)."
if [ "$KEEP_MIGRATION_SERVICE" = "true" ]; then
  print_migration_resync_hints
fi
print_geo_file_generation_hints
echo "Rebuild only frontend: docker compose up -d --build dsp-frontend"
echo "Reset DBs (lose data): docker compose down -v && ./setup.sh"
echo ""
