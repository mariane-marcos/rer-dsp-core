#!/usr/bin/env bash
# Configure the adopter without exposing internal DSP file keys.
# Generates installation-config.json, mapLayersConfig.json,
# downloadThemesConfig.json and application.yaml via apply_adopter_config.py.
# Usage: ./config.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
CONFIG_FILE="$ROOT_DIR/config/adopter/adopter-config.yaml"
EXAMPLE_FILE="$ROOT_DIR/config/adopter/adopter-config.yaml.example"
APPLY="$ROOT_DIR/scripts/apply_adopter_config.py"

print_config_rebuild_hint() {
  echo ""
  echo "Operational files were written under config/."
  echo "They are copied into Docker images on the next build."
  echo "Run ./setup.sh or ./start.sh so containers pick up this configuration."
  echo ""
}

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/common.sh"

print_config_intro() {
  echo ""
  echo "Guided Adopter Configuration"
  echo ""
  echo "This wizard collects adopter-specific settings in this order: source database, tables and jobs, application settings, then interface."
  echo "Your configuration is stored in:"
  echo "  $CONFIG_FILE"
  echo ""
  echo "That YAML file is the single source of truth used to generate the operational DSP configuration files."
  echo ""
  echo "Ways to work with this file:"
  echo "  • Use this wizard — step-by-step prompts write answers to the path above"
  echo "  • Bring your own file — copy your adopter-config.yaml to that path, then run ./config.sh and choose 1 (reapply)"
  echo "  • Edit manually — change the YAML in any editor, then run ./config.sh and choose 1 (reapply)"
  echo ""
  echo "If the file already exists, the menu below also offers guided edit (option 2) or start over from the template (option 3)."
  echo ""
}

if [ "$#" -gt 0 ]; then
  error "Run ./config.sh with no arguments."
  exit 1
fi

if [ ! -f "$EXAMPLE_FILE" ]; then
  error "Template not found: $EXAMPLE_FILE"
  exit 1
fi

ensure_dotenv
ensure_dsp_repositories --backend --frontend --job --geo-file-job
print_config_intro

if [ -f "$CONFIG_FILE" ]; then
  echo ""
  echo "An existing configuration was found."
  echo ""
  echo "What do you want to do?"
  echo "  1) Reapply the existing configuration (no wizard)"
  echo "  2) Edit the existing configuration (wizard with current values)"
  echo "  3) Start over from the template (discards current file)"
  echo ""
  choice=""
  read -e -r -p "Choice [1/2/3]: " choice || true
  case "$choice" in
    1)
      python3 "$APPLY" --root "$ROOT_DIR" --config "$CONFIG_FILE"
      print_config_rebuild_hint
      exit 0
      ;;
    2)
      python3 "$APPLY" --root "$ROOT_DIR" --config "$CONFIG_FILE" --wizard --edit
      print_config_rebuild_hint
      exit 0
      ;;
    3)
      rm -f "$CONFIG_FILE"
      ;;
    *)
      error "Invalid choice: '${choice}' — use 1 (reapply), 2 (edit), or 3 (start over)."
      exit 1
      ;;
  esac
fi

python3 "$APPLY" --root "$ROOT_DIR" --config "$CONFIG_FILE" --wizard
print_config_rebuild_hint
