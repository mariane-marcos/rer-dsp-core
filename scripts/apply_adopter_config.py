#!/usr/bin/env python3
"""Generate operational files from the adopter configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import select
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

# URLs consumed by the browser, so they go through the gateway (DSP_PUBLIC_BASE_URL in .env).
PUBLIC_BASE_URL = os.environ.get("DSP_PUBLIC_BASE_URL", "http://localhost:8026").rstrip("/")
FIXED_WMS_BASE_URL = f"{PUBLIC_BASE_URL}/geoserver-exhibition/dsp/wms"
# Downloads use GeoServer Download (separate from map Exhibition WMS).
FIXED_WFS_BASE_URL = f"{PUBLIC_BASE_URL}/geoserver-download/dsp/wfs"
HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
LAYER_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DESTINATION_SCHEMA = "dsp"
FIXED_GROUP_KEYS = {
    "territorial_division": "dt",
    "areas_of_interest": "ird",
}
FIXED_LAYER_IDS = {
    "dsp:territory-level-1",
    "dsp:territory-level-2",
    "dsp:territory-level-3",
    "dsp:area-of-interest",
}
# Target column names reserved for layers (same as LayerConfig.CANONICAL_TARGET_COLUMNS).
LAYER_CANONICAL_TARGET_COLUMNS = (
    "id",
    "area_of_interest_id",
    "created_at",
    "updated_at",
    "label",
    "geom",
)
# Target column names reserved for AOI (same as AreaOfInterestConfig.CANONICAL_TARGET_COLUMNS).
AOI_CANONICAL_TARGET_COLUMNS = (
    "id",
    "created_at",
    "updated_at",
    "territory_level_3_id",
    "area",
    "geom",
)
# Defaults of environment.object_storage, used when the block is absent from an
# adopter-config.yaml written before the pre-generation feature existed.
OBJECT_STORAGE_DEFAULTS = {
    "endpoint": "",
    "region": "us-east-1",
    "bucket": "dsp-geo-files",
    "access_key": "",
    "secret_key": "",
    "path_style_access": True,
    "generation_cron": "0 2 * * *",
}
RESERVED_DESTINATION_TABLES = {
    "territory_level_1",
    "territory_level_2",
    "territory_level_3",
    "area_of_interest",
}
CANONICAL_AOI_DETAIL_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "area",
)
CALCULATED_AOI_DETAIL_FIELDS = (
    "calculated.latitude",
    "calculated.longitude",
    "calculated.territory_level_2_name",
    "calculated.territory_level_3_name",
)
FORBIDDEN_AOI_DETAIL_FIELDS = frozenset(
    {
        "theme_1",
        "theme_2",
        "theme_3",
        "theme_4",
        "geom",
        "geometry",
        "boundary_box",
        "centroid_coordinates",
    }
)

try:
    import yaml
except ImportError:
    print("Error: the Python 'yaml' module is required. Install PyYAML.", file=sys.stderr)
    raise SystemExit(1)


class AdopterConfigDumper(yaml.SafeDumper):
    """Keys stay unquoted; strings use default YAML quoting (only when needed)."""

    def represent_mapping(self, tag, mapping, flow_style=None):
        node = super().represent_mapping(tag, mapping, flow_style)
        for key_node, _value_node in node.value:
            if (
                isinstance(key_node, yaml.ScalarNode)
                and key_node.tag == "tag:yaml.org,2002:str"
            ):
                key_node.style = None
        return node


def represent_adopter_string(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=">")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


AdopterConfigDumper.add_representer(str, represent_adopter_string)


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=AdopterConfigDumper,
        allow_unicode=True,
        sort_keys=False,
    )


def write_adopter_config(
    path: Path, values: dict[str, Any], template: dict[str, Any]
) -> None:
    """Write adopter YAML using the template key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(deep_merge(template, values)), encoding="utf-8")


def read_dotenv_value(env_file: Path, key: str, default: str = "") -> str:
    if not env_file.is_file():
        return default
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            env_key, _, value = stripped.partition("=")
            if env_key == key:
                return value
    return default


def get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key, default)
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Fill missing keys from base without overwriting override values."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_clipboard() -> str:
    """Return clipboard text when a host tool is available."""
    for cmd in (
        ["wl-paste", "-n"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
    ):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return ""


def _sanitize_pasted_text(text: str) -> str:
    """Keep wizard answers on one line."""
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n", 1)[0]


_stdin_pushback: list[str] = []


def _read_char() -> str:
    """Read one stdin character, honoring any pushback from prior reads."""
    if _stdin_pushback:
        return _stdin_pushback.pop(0)
    return sys.stdin.read(1)


def _consume_optional_lf() -> None:
    """Drop a trailing LF after CR so it does not answer the next prompt."""
    if not select.select([sys.stdin], [], [], 0)[0]:
        return
    ch = sys.stdin.read(1)
    if ch != "\n":
        _stdin_pushback.append(ch)


def _read_bracketed_paste() -> str:
    """Read terminal bracketed paste payload until ESC [ 201 ~."""
    parts: list[str] = []
    while True:
        ch = _read_char()
        if ch != "\x1b":
            parts.append(ch)
            continue
        if _read_char() != "[":
            parts.append("\x1b")
            continue
        param = ""
        while True:
            code = _read_char()
            if code == "~":
                if param == "201":
                    return "".join(parts)
                parts.append("\x1b[" + param + "~")
                break
            param += code


def _read_interactive_line(prompt: str) -> str:
    """Read one line with arrow keys, backspace/delete and Ctrl+V paste."""
    if termios is None or tty is None or not sys.stdin.isatty():
        return input(prompt)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buffer: list[str] = []
    cursor = 0

    def redraw() -> None:
        sys.stdout.write("\r" + prompt + "".join(buffer))
        sys.stdout.write("\x1b[K")
        tail = len(buffer) - cursor
        if tail:
            sys.stdout.write(f"\x1b[{tail}D")
        sys.stdout.flush()

    def insert_text(text: str) -> None:
        nonlocal cursor
        if not text:
            return
        buffer[cursor:cursor] = list(text)
        cursor += len(text)
        redraw()

    def delete_forward() -> None:
        nonlocal cursor
        if cursor < len(buffer):
            del buffer[cursor]
            redraw()

    def delete_backward() -> None:
        nonlocal cursor
        if cursor > 0:
            del buffer[cursor - 1]
            cursor -= 1
            redraw()

    sys.stdout.write(prompt)
    sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        while True:
            ch = _read_char()
            if ch == "\n":
                break
            if ch == "\r":
                _consume_optional_lf()
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x04" and not buffer:
                break
            if ch == "\x16":  # Ctrl+V
                insert_text(_sanitize_pasted_text(_read_clipboard()))
                continue
            if ch in ("\x7f", "\x08"):
                delete_backward()
                continue
            if ch == "\x1b":
                if _read_char() != "[":
                    continue
                param = ""
                while True:
                    code = _read_char()
                    if code == "~":
                        if param == "200":
                            insert_text(_sanitize_pasted_text(_read_bracketed_paste()))
                        elif param == "3":
                            delete_forward()
                        break
                    if code.isalpha() and not param:
                        if code == "D" and cursor > 0:
                            cursor -= 1
                            sys.stdout.write("\x1b[D")
                            sys.stdout.flush()
                        elif code == "C" and cursor < len(buffer):
                            cursor += 1
                            sys.stdout.write("\x1b[C")
                            sys.stdout.flush()
                        break
                    param += code
                continue
            if ch.isprintable() or ch == "\t":
                insert_text(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(buffer)


def ask(label: str, default: Any = "") -> Any:
    suffix = f" [{default}]" if default not in ("", None) else ""
    print(f"{label}{suffix}:")
    answer = _read_interactive_line("  > ").strip()
    return default if answer == "" else answer


def ask_bool(label: str, default: bool) -> bool:
    value = str(ask(f"{label} (y/n)", "y" if default else "n")).lower()
    return value in {"y", "yes"}


def ask_field(label: str, default: Any, description: str, used_in: str) -> Any:
    print(f"\n  {label}")
    print(f"  What: {description}")
    print(f"  Used in: {used_in}")
    return ask("  Value", default)


def ask_optional_field(label: str, default: Any, description: str, used_in: str) -> str | None:
    print(f"\n  {label}")
    print(f"  What: {description}")
    print(f"  Used in: {used_in}")
    print("  Skip: press Enter if this column is not available.")
    raw = ask("  Value", default)
    text = str(raw or "").strip()
    if not text or text.lower() == "null" or "<" in text:
        return None
    return text


def ask_int_field(
    label: str,
    default: Any,
    description: str,
    used_in: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    while True:
        print(f"\n  {label}")
        print(f"  What: {description}")
        print(f"  Used in: {used_in}")
        raw = ask("  Value", default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            print("\n  Enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            print(f"\n  Enter a value greater than or equal to {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"\n  Enter a value less than or equal to {maximum}.")
            continue
        return value


def ask_float_field(
    label: str,
    default: Any,
    description: str,
    used_in: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    while True:
        print(f"\n  {label}")
        print(f"  What: {description}")
        print(f"  Used in: {used_in}")
        raw = ask("  Value", default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            print("\n  Enter a numeric value.")
            continue
        if not (value == value and abs(value) != float("inf")):
            print("\n  Enter a finite numeric value.")
            continue
        if minimum is not None and value < minimum:
            print(f"\n  Enter a value greater than or equal to {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"\n  Enter a value less than or equal to {maximum}.")
            continue
        return value


def ask_color_field(
    label: str,
    default: Any,
    description: str,
    used_in: str,
    *,
    allow_transparent: bool = False,
) -> str:
    while True:
        print(f"\n  {label}")
        print(f"  What: {description}")
        print(f"  Used in: {used_in}")
        raw = str(ask("  Value", default)).strip()
        if allow_transparent and raw == "transparent":
            return raw
        if HEX_COLOR.fullmatch(raw):
            return raw
        if allow_transparent:
            print("\n  Enter a #RGB or #RRGGBB value, or 'transparent'.")
        else:
            print("\n  Enter a #RGB or #RRGGBB value.")


def ask_bool_field(label: str, default: bool, description: str, used_in: str) -> bool:
    print(f"\n  {label}")
    print(f"  What: {description}")
    print(f"  Used in: {used_in}")
    return ask_bool("  Value", default)


def etl_field_help(field: str) -> str:
    descriptions = {
        "source_table": (
            "Source table name (schema.table) for this wizard. "
            "SQL subqueries are also supported, but only by editing "
            "./config/adopter/adopter-config.yaml (advanced mode), then running "
            "./config.sh — do not type SQL here."
        ),
        "primary_key": (
            "A single source column that uniquely identifies each record. "
            "Composite keys (two or more columns) are not supported — "
            "the destination id and the WFS/CSV FID use this column alone."
        ),
        "parent_key": "Source column linking this record to its parent territory.",
        "layer_parent_key": (
            "Source column linking this feature to its area of interest (AOI)."
        ),
        "name_column": "Source column containing the display name.",
        "geometry_column": "Source column containing the geometry.",
        "created_at_column": "Source column containing the creation timestamp.",
        "updated_at_column": (
            "Source column with the last modification timestamp (optional). "
        ),
        "label_column": (
            "Source column with the feature display name (optional). "
        ),
        "territory_level_3_column": "Source column linking an area to territorial level 3.",
        "area_column": "Source column containing the area measurement.",
        "additional_columns": (
            "Other source columns to copy besides the required ones. "
            "The destination keeps the same names."
        ),
        "business_only_persist_columns": (
            "Source columns migrated only to dsp-db for theme KPIs "
            "(theme_1 … theme_N; use SQL aliases in source_table when names differ)."
        ),
    }
    return descriptions.get(field, "Source value used by the ETL mapping.")


def aoi_additional_columns(values: dict[str, Any]) -> list[str]:
    """Extra source columns selected for AOI migration (business + geo-target)."""
    aoi = get(values, "etl", "area_of_interest", default={})
    if not isinstance(aoi, dict):
        return []
    return parse_column_list(aoi.get("additional_columns"), "etl.area_of_interest.additional_columns")


def aoi_business_only_persist_columns(values: dict[str, Any], theme_count: int) -> list[str]:
    aoi = get(values, "etl", "area_of_interest", default={})
    if not isinstance(aoi, dict):
        return validate_business_only_persist_columns([], theme_count, "etl.area_of_interest.business_only_persist_columns")
    return validate_business_only_persist_columns(
        aoi.get("business_only_persist_columns"),
        theme_count,
        "etl.area_of_interest.business_only_persist_columns",
    )


def aoi_detail_field_options(additional_columns: list[str]) -> list[str]:
    """Valid details-panel keys: additional_columns ∪ canonical ∪ calculated.*."""
    options: list[str] = []
    seen: set[str] = set()
    for name in (
        list(additional_columns)
        + list(CANONICAL_AOI_DETAIL_FIELDS)
        + list(CALCULATED_AOI_DETAIL_FIELDS)
    ):
        if name in seen:
            continue
        seen.add(name)
        options.append(name)
    return options


def validate_aoi_detail_fields(
    values: dict[str, Any],
    additional_columns: list[str],
) -> list[dict[str, str]]:
    """Validate details.fields: AOI migration columns or calculated.* keys."""
    prefix = "installation.screens.detail.fields"
    detail = get(values, "installation", "screens", "detail", default={})
    raw = detail.get("fields") if isinstance(detail, dict) else None
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{prefix} must be a list of field/label mappings.")

    extras = set(additional_columns)
    calculated = set(CALCULATED_AOI_DETAIL_FIELDS)
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        item_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_prefix} must be a mapping with 'field' and 'label'.")
        field = item.get("field")
        label = item.get("label")
        if not isinstance(field, str) or not field.strip():
            raise ValueError(f"{item_prefix}.field is required.")
        field = field.strip()
        if "<" in field:
            raise ValueError(f"{item_prefix}.field still contains a placeholder.")
        if field in seen:
            raise ValueError(f"{prefix}: duplicate field '{field}'.")
        seen.add(field)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"{item_prefix}.label is required.")
        label = label.strip()
        if "<" in label:
            raise ValueError(f"{item_prefix}.label still contains a placeholder.")
        if field in FORBIDDEN_AOI_DETAIL_FIELDS:
            raise ValueError(
                f"{item_prefix}.field '{field}' cannot be shown on the AOI details panel."
            )
        if field.startswith("calculated."):
            if field not in calculated:
                raise ValueError(
                    f"{item_prefix}.field '{field}' is not a calculated details field. "
                    "Use calculated.latitude, calculated.longitude, "
                    "calculated.territory_level_2_name, or calculated.territory_level_3_name."
                )
            normalized.append({"field": field, "label": label})
            continue
        if field in CANONICAL_AOI_DETAIL_FIELDS:
            normalized.append({"field": field, "label": label})
            continue
        if field not in extras:
            raise ValueError(
                f"{item_prefix}.field '{field}' is not in "
                "etl.area_of_interest.additional_columns, is not a canonical AOI column "
                "(id, created_at, updated_at, area), and is not a calculated "
                "field (calculated.latitude, calculated.longitude, "
                "calculated.territory_level_2_name, calculated.territory_level_3_name). "
                "Select it for AOI migration first, or remove it from details."
            )
        normalized.append({"field": field, "label": label})
    return normalized


def ask_aoi_detail_fields(config: dict[str, Any]) -> None:
    """Prompt for details-panel fields after additional_columns is chosen."""
    additional = parse_column_list(
        config["etl"]["area_of_interest"].get("additional_columns") or [],
        "etl.area_of_interest.additional_columns",
    )
    options = aoi_detail_field_options(additional)
    options_set = set(options)
    screens = config.setdefault("installation", {}).setdefault("screens", {})
    detail = screens.setdefault("detail", {})
    existing = detail.get("fields") or []
    default_fields: list[str] = []
    labels_by_field: dict[str, str] = {}
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            name = str(item.get("field") or "").strip()
            if not name:
                continue
            default_fields.append(name)
            existing_label = item.get("label")
            if isinstance(existing_label, str) and existing_label.strip():
                labels_by_field[name] = existing_label.strip()

    identifier = screens.get("identifier") if isinstance(screens.get("identifier"), dict) else {}
    hierarchy = get(config, "installation", "hierarchy", default={})
    level2 = hierarchy.get("level2") if isinstance(hierarchy, dict) else {}
    level3 = hierarchy.get("level3") if isinstance(hierarchy, dict) else {}
    if not isinstance(level2, dict):
        level2 = {}
    if not isinstance(level3, dict):
        level3 = {}
    canonical_label_defaults = {
        "id": identifier.get("label") or "Identifier",
        "created_at": detail.get("registration_date_label") or "Registration date",
        "updated_at": detail.get("alteration_date_label") or "Alteration date",
        "area": detail.get("area_label") or "Area",
        "calculated.latitude": detail.get("latitude_label") or "Latitude",
        "calculated.longitude": detail.get("longitude_label") or "Longitude",
        "calculated.territory_level_2_name": level2.get("label") or "Level 2",
        "calculated.territory_level_3_name": level3.get("label") or "Level 3",
    }

    while True:
        print("\n  AOI details fields")
        print(
            "  What: Fields shown on the area of interest details panel, in this order. "
            "Mix migrated columns with calculated.* keys."
        )
        print("  Used in: the home screen detail panel")
        print(
            "  calculated.* keys are calculated by the application, not a source column: "
            + ", ".join(CALCULATED_AOI_DETAIL_FIELDS)
        )
        print("  Options: " + ", ".join(options))
        print("  How: type field names separated by commas (order is kept).")
        print("  Skip: press Enter to keep the built-in details list.")
        raw = ask("  Fields", ", ".join(default_fields))
        text = str(raw or "").strip()
        if text and "," not in text and any(char.isspace() for char in text):
            print(
                "\n  Separate names with commas, not only spaces. "
                "Example: id, nome, calculated.latitude"
            )
            continue
        try:
            selected = parse_column_list(text, "installation.screens.detail.fields")
        except ValueError as exc:
            print(f"\n  {exc}")
            continue
        unknown = [name for name in selected if name not in options_set]
        if unknown:
            print(
                f"\n  These fields are not available: {', '.join(unknown)}. "
                "Choose only from columns selected for AOI migration, "
                "canonical columns (id, created_at, updated_at, area), "
                "and calculated.* keys."
            )
            continue
        break

    fields: list[dict[str, str]] = []
    for name in selected:
        default_label = labels_by_field.get(name) or canonical_label_defaults.get(name) or name
        help_text = f"Label shown on the details panel for '{name}'."
        if name.startswith("calculated."):
            help_text += " This value is calculated by the application, not a source column."
        label = ask_field(
            f"AOI details label — {name}",
            default_label,
            help_text,
            "the home screen detail panel",
        )
        fields.append({"field": name, "label": str(label).strip()})
    detail["fields"] = fields


def parse_column_list(raw: Any, prefix: str) -> list[str]:
    """Parse optional extra columns from a list or a comma-separated string."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        parts = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list):
        parts = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{prefix} must not contain blank entries.")
            parts.append(item.strip())
    else:
        raise ValueError(f"{prefix} must be a list of column names or a comma-separated string.")
    seen: set[str] = set()
    unique: list[str] = []
    for column in parts:
        if "<" in column:
            raise ValueError(f"{prefix} still contains a placeholder ('{column}').")
        if column in seen:
            raise ValueError(f"{prefix}: duplicate column '{column}'.")
        seen.add(column)
        unique.append(column)
    return unique


def columns_clashing_with(
    columns: list[str],
    blocked: Any,
    *,
    ignore_case: bool = False,
) -> list[str]:
    """Return columns from `columns` that are already in the blocked set, in original order."""
    if ignore_case:
        blocked_lower = {str(name).lower() for name in blocked if name}
        return [column for column in columns if column.lower() in blocked_lower]
    blocked_set = set(blocked)
    return [column for column in columns if column in blocked_set]


def canonical_target_clash_reason(target_names: Any) -> str:
    listed = ", ".join(sorted(target_names))
    return f"collides with a canonical target column name [{listed}]."


def reject_clashing_columns(
    columns: list[str],
    blocked: Any,
    prefix: str,
    *,
    reason: str = "duplicates a required column mapping.",
    ignore_case: bool = False,
) -> None:
    for column in columns_clashing_with(columns, blocked, ignore_case=ignore_case):
        raise ValueError(f"{prefix} entry '{column}' {reason}")


def reject_source_and_target_clashes(
    columns: list[str],
    source_blocked: Any,
    target_blocked: Any,
    prefix: str,
    *,
    target_ignore_case: bool = False,
    source_reason: str = "duplicates a required column mapping.",
) -> None:
    reject_clashing_columns(columns, source_blocked, prefix, reason=source_reason)
    reject_clashing_columns(
        columns,
        target_blocked,
        prefix,
        reason=canonical_target_clash_reason(target_blocked),
        ignore_case=target_ignore_case,
    )


def ask_optional_column_list(label: str, default: Any, description: str, used_in: str) -> list[str]:
    if isinstance(default, list):
        default_display = ", ".join(default)
    else:
        default_display = str(default or "")
    while True:
        print(f"\n  {label}")
        print(f"  What: {description}")
        print(f"  Used in: {used_in}")
        print("  How: type column names separated by commas.")
        print("  Example: owner, status, year")
        print("  Skip: press Enter if there are no extra columns.")
        raw = ask("  Columns", default_display)
        text = str(raw or "").strip()
        if text and "," not in text and any(char.isspace() for char in text):
            print(
                "\n  Separate names with commas, not only spaces. "
                "Example: owner, status, year"
            )
            continue
        try:
            return parse_column_list(text, label)
        except ValueError as exc:
            print(f"\n  {exc}")


def _without_source_or_target(
    columns: list[str],
    source_blocked: Any,
    target_blocked: Any,
    *,
    target_ignore_case: bool = False,
) -> list[str]:
    source_set = {name for name in source_blocked if name}
    return [
        column
        for column in columns
        if column not in source_set
        and not columns_clashing_with(
            [column], target_blocked, ignore_case=target_ignore_case
        )
    ]


def ask_unblocked_column_list(
    label: str,
    default: Any,
    description: str,
    used_in: str,
    blocked: Any,
    prefix: str,
    *,
    clash_reason: str = "duplicates a required column mapping.",
    target_blocked: Any = (),
    target_ignore_case: bool = False,
) -> list[str]:
    """Prompt for a column list and reject names already mapped or reserved on the target side."""
    source_set = {name for name in blocked if name}
    try:
        suggested = _without_source_or_target(
            parse_column_list(default or [], prefix),
            source_set,
            target_blocked,
            target_ignore_case=target_ignore_case,
        )
    except ValueError:
        suggested = []
    while True:
        selected = ask_optional_column_list(label, suggested, description, used_in)
        source_clash = columns_clashing_with(selected, source_set)
        if source_clash:
            print(f"\n  Already mapped (do not list again): {', '.join(source_clash)}")
            if clash_reason:
                print(f"  {prefix} entry '{source_clash[0]}' {clash_reason}")
            suggested = _without_source_or_target(
                selected, source_set, target_blocked, target_ignore_case=target_ignore_case
            )
            continue
        target_clash = columns_clashing_with(
            selected, target_blocked, ignore_case=target_ignore_case
        )
        if target_clash:
            reason = canonical_target_clash_reason(target_blocked)
            print(f"\n  Reserved destination name (do not list): {', '.join(target_clash)}")
            print(f"  {prefix} entry '{target_clash[0]}' {reason}")
            suggested = _without_source_or_target(
                selected, source_set, target_blocked, target_ignore_case=target_ignore_case
            )
            continue
        return selected


def require_non_blank_column(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not value.strip() or "<" in value:
        raise ValueError(f"{prefix} is required (source column name, no placeholder).")
    return value.strip()


def require_simple_primary_key(value: Any, prefix: str) -> str:
    if isinstance(value, (list, tuple)):
        raise ValueError(
            f"{prefix} must be a single column; composite keys are not supported."
        )
    name = require_non_blank_column(value, prefix)
    if "," in name or ";" in name:
        raise ValueError(
            f"{prefix} must be a single column; composite keys are not supported."
        )
    return name


def resolve_optional_column(value: Any, prefix: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{prefix} must be a string or null.")
    stripped = value.strip()
    if not stripped or stripped.lower() == "null":
        return None
    if "<" in stripped:
        raise ValueError(f"{prefix} still contains a placeholder.")
    return stripped


def expected_theme_source_columns(theme_count: int) -> list[str]:
    return [f"theme_{index}" for index in range(1, theme_count + 1)]


def validate_business_only_persist_columns(raw: Any, theme_count: int, prefix: str) -> list[str]:
    columns = parse_column_list(raw or [], prefix)
    if theme_count == 0:
        if columns:
            raise ValueError(f"{prefix} must be empty when installation.kpis.theme_count is 0.")
        return []
    if len(columns) != theme_count:
        raise ValueError(
            f"{prefix} must contain exactly {theme_count} column name(s); found {len(columns)}."
        )
    expected = set(expected_theme_source_columns(theme_count))
    if set(columns) != expected:
        raise ValueError(
            f"{prefix} must list theme_1 … theme_{theme_count} "
            "(source column names or SQL aliases in source_table). "
            f"Found: {', '.join(columns)}."
        )
    return expected_theme_source_columns(theme_count)


def resolve_optional_layer_field(entry: dict[str, Any], field: str, prefix: str) -> str | None:
    value = entry.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}.{field} must be a non-empty string.")
    if "<" in value:
        raise ValueError(f"{prefix}.{field} still contains a placeholder.")
    return value.strip()


def require_layer_field(entry: dict[str, Any], field: str, prefix: str) -> str:
    value = resolve_optional_layer_field(entry, field, prefix)
    if value is None:
        raise ValueError(f"{prefix}.{field} is required.")
    return value


def reset_disabled_themes(
    config: dict[str, Any],
    template: dict[str, Any],
    theme_count: int,
) -> bool:
    """Restore template defaults for themes above theme_count and clear ETL columns."""
    changed = False
    kpis = config["installation"]["kpis"]
    template_kpis = template["installation"]["kpis"]
    for index in range(1, 5):
        code = f"theme_{index}"
        if index > theme_count:
            restored = copy.deepcopy(template_kpis[code])
            restored["enabled"] = False
            if kpis.get(code) != restored:
                changed = True
            kpis[code] = restored
        elif not kpis.get(code, {}).get("enabled", True):
            kpis[code]["enabled"] = True
            changed = True

    aoi = config["etl"]["area_of_interest"]
    if theme_count == 0:
        if aoi.get("business_only_persist_columns"):
            aoi["business_only_persist_columns"] = []
            changed = True
    return changed


def enabled_kpi_codes(theme_count: int) -> tuple[str, ...]:
    return ("area_of_interest",) + tuple(f"theme_{index}" for index in range(1, theme_count + 1))


MAP_VIEW_MODES = ("territorial_bbox", "manual", "planet")
PLANET_CENTER_LAT = 0.0
PLANET_CENTER_LNG = 0.0
PLANET_ZOOM = 0


def is_source_table_placeholder(source_table: str) -> bool:
    normalized = source_table.strip()
    return not normalized or "<source_" in normalized or normalized == "source_schema.source_table"


def is_territory_level_etl_configured(values: dict[str, Any], level: str) -> bool:
    source_table = str(get(values, "etl", level, "source_table", default=""))
    return not is_source_table_placeholder(source_table)


def is_level1_source_configured(values: dict[str, Any]) -> bool:
    return is_territory_level_etl_configured(values, "level1")


def is_territorial_bbox_viable(values: dict[str, Any]) -> bool:
    """True when at least one territorial level can feed GET /territory/boundary-box."""
    return any(
        is_territory_level_etl_configured(values, level)
        for level in ("level1", "level2", "level3")
    )


TERRITORIAL_BBOX_PLANET_REASON = (
    "Territorial geometry cannot be resolved from the ETL configuration "
    "(no configured level1/level2/level3 source table)."
)


def coerce_map_initial_view_to_planet(values: dict[str, Any]) -> bool:
    """Switch territorial_bbox to planet when the frontend would fall back to the globe."""
    initial_view = get(values, "map", "initial_view", default={}) or {}
    if initial_view.get("mode") != "territorial_bbox":
        return False
    if is_territorial_bbox_viable(values):
        return False

    map_config = values.setdefault("map", {})
    initial_view = map_config.setdefault("initial_view", {})
    initial_view["mode"] = "planet"
    initial_view["latitude"] = None
    initial_view["longitude"] = None
    initial_view["zoom"] = None

    print()
    print("  Map initial view: adjusted automatically.")
    print(f"  {TERRITORIAL_BBOX_PLANET_REASON}")
    print("  Changed map.initial_view.mode from 'territorial_bbox' to 'planet'.")
    return True


def _initial_view_coordinate_fields(initial_view: dict[str, Any]) -> dict[str, Any]:
    return {
        name: initial_view.get(name)
        for name in ("latitude", "longitude", "zoom")
    }


def _has_any_coordinate_field(fields: dict[str, Any]) -> bool:
    return any(
        value is not None and not (isinstance(value, str) and not value.strip())
        for value in fields.values()
    )


def _build_initial_view_json(
    mode: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    zoom: int | None = None,
) -> dict[str, Any]:
    return {
        "initialView": {
            "mode": mode,
            "latitude": latitude,
            "longitude": longitude,
            "zoom": zoom,
        }
    }


def validate_map_initial_view(values: dict[str, Any]) -> dict[str, Any]:
    """Validate map.initial_view and return the installation map block."""
    initial_view = get(values, "map", "initial_view", default={}) or {}
    mode = initial_view.get("mode")
    if not isinstance(mode, str) or mode not in MAP_VIEW_MODES:
        raise ValueError(
            "map.initial_view.mode is required and must be one of: "
            + ", ".join(MAP_VIEW_MODES)
            + "."
        )

    coordinate_fields = _initial_view_coordinate_fields(initial_view)
    has_coordinates = _has_any_coordinate_field(coordinate_fields)

    if mode in ("territorial_bbox", "planet"):
        if has_coordinates:
            raise ValueError(
                f"map.initial_view.latitude, longitude, and zoom must be null when "
                f"mode is '{mode}'."
            )
        return _build_initial_view_json(mode)

    latitude = coordinate_fields["latitude"]
    longitude = coordinate_fields["longitude"]
    zoom = coordinate_fields["zoom"]
    if not has_coordinates or any(
        value is None or (isinstance(value, str) and not value.strip())
        for value in coordinate_fields.values()
    ):
        raise ValueError(
            "map.initial_view requires latitude, longitude, and zoom when mode is 'manual'."
        )

    try:
        lat = float(latitude)
        lng = float(longitude)
        zoom_value = int(zoom)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "map.initial_view latitude and longitude must be numbers; zoom must be an integer."
        ) from exc

    if not (-90 <= lat <= 90):
        raise ValueError("map.initial_view.latitude must be between -90 and 90.")
    if not (-180 <= lng <= 180):
        raise ValueError("map.initial_view.longitude must be between -180 and 180.")
    if not (0 <= zoom_value <= 16):
        raise ValueError("map.initial_view.zoom must be an integer between 0 and 16.")

    return _build_initial_view_json(
        "manual",
        latitude=lat,
        longitude=lng,
        zoom=zoom_value,
    )


def ask_map_initial_view(config: dict[str, Any]) -> None:
    """Configure the home map initial view mode."""
    map_config = config.setdefault("map", {})
    initial_view = map_config.setdefault(
        "initial_view",
        {
            "mode": "territorial_bbox",
            "latitude": None,
            "longitude": None,
            "zoom": None,
        },
    )

    print("\n  Initial map view")
    print("  What: Controls how the home map opens and returns to its default view")
    print("        (Center map tool and Clear search on the home page).")
    print("  Used in: the home page map")
    print()
    print("  Choose one mode:")
    print("    1. territorial_bbox — automatic framing from migrated L1/L2/L3 geometries")
    print("    2. manual — fixed latitude, longitude, and zoom")
    print("    3. planet — always show the whole world (zoomed out)")
    print()
    print("  If territorial_bbox is selected but no territorial level (L1/L2/L3) is")
    print("  configured in the ETL, the mode will be set to 'planet' automatically")
    print("  when configuration is applied (same view the frontend would use as fallback).")

    current_mode = initial_view.get("mode", "territorial_bbox")
    if current_mode not in MAP_VIEW_MODES:
        current_mode = "territorial_bbox"

    while True:
        print(f"\n  Current mode: {current_mode}")
        choice = ask("  Mode (1=territorial_bbox, 2=manual, 3=planet)", {
            "territorial_bbox": "1",
            "manual": "2",
            "planet": "3",
        }.get(current_mode, "1"))
        if choice == "1":
            selected_mode = "territorial_bbox"
            break
        if choice == "2":
            selected_mode = "manual"
            break
        if choice == "3":
            selected_mode = "planet"
            break
        print("\n  Enter 1, 2, or 3.")

    initial_view["mode"] = selected_mode

    if selected_mode == "territorial_bbox":
        initial_view["latitude"] = None
        initial_view["longitude"] = None
        initial_view["zoom"] = None
        if not is_territorial_bbox_viable(config):
            print()
            print("  Note: no territorial level (L1/L2/L3) is configured in the ETL yet.")
            print("  If that remains true after the wizard, map.initial_view.mode will be")
            print("  changed to 'planet' automatically when configuration is applied.")
        return

    if selected_mode == "planet":
        initial_view["latitude"] = None
        initial_view["longitude"] = None
        initial_view["zoom"] = None
        return

    initial_view["latitude"] = ask_float_field(
        "Initial center latitude",
        initial_view.get("latitude", -14.2),
        "Decimal degrees between -90 and 90. Example: -14.2 for central Brazil.",
        "the map center on load and when resetting the view",
        minimum=-90,
        maximum=90,
    )
    initial_view["longitude"] = ask_float_field(
        "Initial center longitude",
        initial_view.get("longitude", -51.9),
        "Decimal degrees between -180 and 180. Example: -51.9 for central Brazil.",
        "the map center on load and when resetting the view",
        minimum=-180,
        maximum=180,
    )
    initial_view["zoom"] = ask_int_field(
        "Initial zoom level",
        initial_view.get("zoom", 10),
        "Whole number from 0 (world) to 16 (closest). Higher values zoom in more.",
        "the map framing on load and when resetting the view",
        minimum=0,
        maximum=16,
    )


def ask_kpi_accent_colors(config: dict[str, Any], theme_count: int) -> None:
    print("\n  KPI card colors")
    print("  What: Highlight color shown on each KPI card in the dashboard.")
    print("  Used in: the application dashboard")
    kpis = config["installation"]["kpis"]
    for code in enabled_kpi_codes(theme_count):
        card = kpis[code]
        card["accent_color"] = ask_color_field(
            f"Accent color for {code}", card["accent_color"],
            "Highlight color for the KPI card.",
            "the application dashboard",
        )


def sync_map_layer_names(config: dict[str, Any]) -> bool:
    """Keep territorial layer display names aligned with hierarchy and AOI KPI labels."""
    changed = False
    hierarchy = config["installation"]["hierarchy"]
    layers = config["map"]["layers"]
    for level in ("level1", "level2", "level3"):
        label = hierarchy[level]["label"]
        if layers[level].get("name") != label:
            layers[level]["name"] = label
            changed = True
    aoi_name = config["installation"]["kpis"]["area_of_interest"]["label"]
    if layers["area_of_interest"].get("name") != aoi_name:
        layers["area_of_interest"]["name"] = aoi_name
        changed = True
    return changed


def split_schema_table(source_table: str) -> tuple[str, str]:
    """Split schema.table (exactly one '.'), matching the job QualifiedTable contract."""
    qualified = str(source_table).strip()
    parts = qualified.split(".")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"source_table must be schema.table with exactly one '.' "
            f"(got {source_table!r})."
        )
    return parts[0].strip(), parts[1].strip()


def table_name_from_source(source_table: str) -> str:
    """Return the unqualified table name (schema discarded), matching the job contract."""
    return split_schema_table(source_table)[1]


def source_schema_from_table(source_table: str) -> str:
    """Return the schema part of schema.table."""
    return split_schema_table(source_table)[0]


def validate_layer_name(layer_name: str, prefix: str = "layer_name") -> str:
    """Ensure layer_name is a valid technical WMS id (lowercase, no spaces/accents)."""
    value = layer_name.strip()
    if not LAYER_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{prefix} must match ^[a-z0-9][a-z0-9_-]*$ "
            f"(lowercase letters, digits, hyphens, underscores; got {layer_name!r}). "
            "Use display_name for the human-readable label."
        )
    return value


def validate_source_table_schema(source_table: str, prefix: str) -> None:
    """Reject dsp.* — that schema is reserved for the migration destination."""
    schema = source_schema_from_table(source_table).lower()
    if schema == DESTINATION_SCHEMA:
        table = table_name_from_source(source_table)
        raise ValueError(
            f"{prefix}.source_table must use the origin schema, not '{DESTINATION_SCHEMA}'. "
            f"The job writes extra layers into '{DESTINATION_SCHEMA}' on geo-target. "
            f"Example: public.{table}"
        )


def resolve_layer_name(entry: dict[str, Any]) -> str:
    explicit = entry.get("layer_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return table_name_from_source(entry["source_table"])


def physical_table_name(layer_name: str) -> str:
    """Physical table id: WMS may use hyphens; the table uses underscores."""
    return layer_name.replace("-", "_")


def normalize_epsg(srid: Any) -> str:
    if isinstance(srid, int):
        return f"EPSG:{srid}"
    if isinstance(srid, str):
        value = srid.strip()
        if value.upper().startswith("EPSG:"):
            return f"EPSG:{value.split(':', 1)[1].strip()}"
        if value.isdigit():
            return f"EPSG:{value}"
    raise ValueError(f"Invalid srid value: {srid!r} (expected integer or EPSG:n).")


def resolve_layer_parent_key(entry: dict[str, Any]) -> str | None:
    """Return the AOI parent_key for a generic layer."""
    value = entry.get("parent_key")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _source_column_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.lower() == "null" or "<" in name:
        return None
    return name


def _unique_column_names(values: list[Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _source_column_name(value)
        if name is None or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def layer_canonical_source_columns(entry: dict[str, Any]) -> list[str]:
    """Source columns already mapped in the layer's required fields."""
    return _unique_column_names(
        [
            entry.get("primary_key"),
            resolve_layer_parent_key(entry),
            entry.get("created_at_column"),
            entry.get("geometry_column"),
            entry.get("updated_at_column"),
            entry.get("label_column"),
        ]
    )


def aoi_canonical_source_columns(aoi: dict[str, Any]) -> list[str]:
    """Source columns already mapped in the AOI's required fields."""
    return _unique_column_names(
        [
            aoi.get("primary_key"),
            aoi.get("created_at_column"),
            aoi.get("territory_level_3_column"),
            aoi.get("area_column"),
            aoi.get("geometry_column"),
            aoi.get("updated_at_column"),
        ]
    )


def ask_layer_additional_columns(entry: dict[str, Any]) -> list[str]:
    """Prompt for layer additional_columns and reject names already mapped or reserved on the target side."""
    return ask_unblocked_column_list(
        "Extra columns to migrate",
        entry.get("additional_columns") or [],
        etl_field_help("additional_columns"),
        "the ETL job mapping for this layer",
        layer_canonical_source_columns(entry),
        "additional_columns",
        target_blocked=LAYER_CANONICAL_TARGET_COLUMNS,
        target_ignore_case=True,
    )


def validate_extra_layers(values: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize etl.layers entries."""
    raw_layers = get(values, "etl", "layers", default=[])
    if raw_layers is None:
        return []
    if not isinstance(raw_layers, list):
        raise ValueError("etl.layers must be a list.")

    seen_tables: set[str] = set()
    seen_wms: set[str] = set()
    validated: list[dict[str, Any]] = []

    for index, entry in enumerate(raw_layers):
        prefix = f"etl.layers[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{prefix} must be a mapping.")
        if "enabled" in entry:
            raise ValueError(
                f"{prefix}.enabled is not supported. "
                "Remove the layer from etl.layers when it should not be migrated or shown."
            )

        source_table = entry.get("source_table")
        if not isinstance(source_table, str) or not source_table.strip():
            raise ValueError(f"{prefix}.source_table is required.")
        if "<" in source_table:
            raise ValueError(f"{prefix}.source_table still contains a placeholder.")
        validate_source_table_schema(source_table.strip(), prefix)

        aoi_column = resolve_layer_parent_key(entry)
        if not aoi_column:
            raise ValueError(f"{prefix}.parent_key is required.")

        layer_name = validate_layer_name(resolve_layer_name(entry), f"{prefix}.layer_name")
        table = physical_table_name(layer_name)
        wms_id = f"dsp:{layer_name}"
        if wms_id in FIXED_LAYER_IDS:
            raise ValueError(f"{prefix}: WMS id {wms_id} collides with another layer.")
        if table in RESERVED_DESTINATION_TABLES:
            raise ValueError(
                f"{prefix}: target table dsp.{table} is reserved for a fixed migration."
            )
        if table in seen_tables:
            raise ValueError(f"{prefix}: duplicate target table dsp.{table}.")
        seen_tables.add(table)
        if wms_id in seen_wms:
            raise ValueError(f"{prefix}: WMS id {wms_id} collides with another layer.")
        seen_wms.add(wms_id)

        if "srid" not in entry or entry.get("srid") in (None, ""):
            raise ValueError(f"{prefix}.srid is required.")
        srs = normalize_epsg(entry["srid"])

        display_name = entry.get("display_name") or layer_name
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError(f"{prefix}.display_name must be a non-empty string.")

        group_key = entry.get("group_key") or "extra_layers"
        if not isinstance(group_key, str) or not group_key.strip():
            raise ValueError(f"{prefix}.group_key must be a non-empty string.")
        group_key = group_key.strip()

        color = entry.get("color", "#2563EB")
        fill_color = entry.get("fill_color", "transparent")
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise ValueError(f"{prefix}.color must be a #RGB or #RRGGBB value.")
        if not isinstance(fill_color, str) or (
            fill_color != "transparent" and not HEX_COLOR.fullmatch(fill_color)
        ):
            raise ValueError(
                f"{prefix}.fill_color must be 'transparent' or a #RGB/#RRGGBB value."
            )

        normalized = {
            "source_table": source_table.strip(),
            "area_of_interest_id_column": aoi_column.strip(),
            "primary_key": require_simple_primary_key(
                entry.get("primary_key"), f"{prefix}.primary_key"
            ),
            "created_at_column": require_layer_field(entry, "created_at_column", prefix),
            "updated_at_column": resolve_optional_layer_field(entry, "updated_at_column", prefix),
            "label_column": resolve_optional_layer_field(entry, "label_column", prefix),
            "geometry_column": require_layer_field(entry, "geometry_column", prefix),
            "layer_name": layer_name,
            "table": table,
            "wms_id": wms_id,
            "srs": srs,
            "srid": int(srs.split(":", 1)[1]),
            "display_name": display_name.strip(),
            "group_key": group_key,
            "active_default": bool(entry.get("active_default", False)),
            "color": color,
            "fill_color": fill_color,
            "where_clause": str(entry.get("where_clause") or "1=1"),
        }
        extras = parse_column_list(
            entry.get("additional_columns"),
            f"{prefix}.additional_columns",
        )
        reject_source_and_target_clashes(
            extras,
            layer_canonical_source_columns(entry),
            LAYER_CANONICAL_TARGET_COLUMNS,
            f"{prefix}.additional_columns",
            target_ignore_case=True,
        )
        if extras:
            normalized["additional_columns"] = extras
        validated.append(normalized)
    return validated


def build_extra_map_layer(entry: dict[str, Any], group_json_key: str) -> dict[str, Any]:
    layer_name = entry["layer_name"]
    stable_key = f"{group_json_key}_{layer_name}".replace("-", "_")
    active = bool(entry["active_default"])
    return {
        "baseUrl": FIXED_WMS_BASE_URL,
        "layers": entry["wms_id"],
        "format": "image/png",
        "transparent": True,
        "name": entry["display_name"],
        "activeDefault": active,
        "active": active,
        "key": stable_key,
        "nativeName": entry["table"],
        "srs": entry["srs"],
        "toggle": {"active": "On", "inactive": "Off"},
        "style": {
            "color": entry["color"],
            "fillColor": entry["fill_color"],
        },
    }


ABOUT_MARKDOWN_SUFFIXES = {".md", ".markdown"}


def slugify_about_filename(label: str, suffix: str, fallback: str) -> str:
    """Build a destination file name from the tab label."""
    normalized = unicodedata.normalize("NFKD", label)
    ascii_label = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_label.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = fallback
    ext = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
    if ext not in ABOUT_MARKDOWN_SUFFIXES:
        ext = ".md"
    return f"{slug}{ext}"


def resolve_about_source_path(raw: str) -> Path:
    """Resolve an absolute path, a CWD-relative path, or a ~/ path."""
    return Path(raw).expanduser().resolve()


def is_inside_about_dir(path: Path, about_dir: Path) -> bool:
    """True when the file sits directly in config/about/ (not a subfolder)."""
    try:
        return path.resolve().parent == about_dir.resolve()
    except OSError:
        return False


def is_safe_about_filename(file_name: str) -> bool:
    """Accept only a Markdown file name, with no folder, ~, or absolute path."""
    if not isinstance(file_name, str):
        return False
    name = file_name.strip()
    if not name or name != Path(name).name:
        return False
    if name in {".", ".."} or "~" in name:
        return False
    if Path(name).is_absolute():
        return False
    return Path(name).suffix.lower() in ABOUT_MARKDOWN_SUFFIXES


def about_tab_destination(label: str, source: Path, about_dir: Path, fallback: str) -> str:
    """File name in config/about/: keep the current name if already there, else slug the label."""
    if is_inside_about_dir(source, about_dir):
        return source.name
    return slugify_about_filename(label, source.suffix, fallback)


def build_about_config(values: dict[str, Any], about_dir: Path) -> dict[str, Any]:
    """Build about-config.json from about.* adopter settings."""
    about = get(values, "about", default={}) or {}
    enabled = bool(about.get("enabled", False))
    if not enabled:
        return {"enabled": False, "bannerTitle": "About", "tabs": []}

    raw_tabs = about.get("tabs")
    if not isinstance(raw_tabs, list) or not raw_tabs:
        raise ValueError("about.tabs must have at least one entry when about.enabled is true.")

    tabs: list[dict[str, str]] = []
    for index, tab in enumerate(raw_tabs):
        prefix = f"about.tabs[{index}]"
        if not isinstance(tab, dict):
            raise ValueError(f"{prefix} must be a mapping with label and file.")
        label = tab.get("label")
        file_name = tab.get("file")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"{prefix}.label is required.")
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError(f"{prefix}.file is required.")
        file_name = file_name.strip()
        if not is_safe_about_filename(file_name):
            raise ValueError(
                f"{prefix}.file '{file_name}' must be a .md or .markdown file name "
                f"inside {about_dir} (no folders or absolute paths). "
                "Use ./config.sh to copy a file from elsewhere."
            )
        if not (about_dir / file_name).is_file():
            raise ValueError(
                f"{prefix}.file '{file_name}' does not exist in {about_dir}."
            )
        tabs.append(
            {
                "id": f"tab-{index + 1}",
                "label": label.strip(),
                "file": file_name,
            }
        )

    return {
        "enabled": True,
        "bannerTitle": str(about.get("banner_title") or "About"),
        "tabs": tabs,
    }


def layer_name_to_code(layer_name: str) -> str:
    return layer_name.replace("-", "_")


def build_download_themes_config(
    values: dict[str, Any],
    extra_layers: list[dict[str, Any]],
) -> dict[str, Any]:
    layer_values = get(values, "map", "layers", default={})
    aoi_override = layer_values.get("area_of_interest", {})
    aoi_name = aoi_override.get("name", "Area of interest")

    themes: list[dict[str, Any]] = [
        {
            "code": "area_of_interest",
            "name": aoi_name,
            "typeName": "dsp:area-of-interest",
            "formats": ["csv"],
            "enabled": True,
            "territoryFilter": {
                "strategy": "direct",
                "level3Field": "territory_level_3_id",
            },
        }
    ]

    for entry in extra_layers:
        layer_name = entry["layer_name"]
        themes.append(
            {
                "code": layer_name_to_code(layer_name),
                "name": entry["display_name"],
                "typeName": entry["wms_id"],
                "formats": ["csv"],
                "enabled": True,
                "territoryFilter": {
                    "strategy": "aoi_linked",
                    "aoiLinkField": "area_of_interest_id",
                },
            }
        )

    return {
        "wfsBaseUrl": FIXED_WFS_BASE_URL,
        "themes": themes,
    }


def append_extra_layers_to_map(
    layers_doc: dict[str, Any],
    values: dict[str, Any],
    extra_layers: list[dict[str, Any]],
) -> None:
    """Append generic layers into mapLayersConfig groups (creates groups as needed)."""
    if not extra_layers:
        return

    group_names = get(values, "map", "group_names", default={}) or {}
    groups_by_key: dict[str, dict[str, Any]] = {
        group["key"]: group for group in layers_doc.get("groups", [])
    }

    for entry in extra_layers:
        adopter_group_key = entry["group_key"]
        json_key = FIXED_GROUP_KEYS.get(adopter_group_key, adopter_group_key)
        group = groups_by_key.get(json_key)
        if group is None:
            display = group_names.get(adopter_group_key, adopter_group_key.replace("_", " ").title())
            group = {
                "name": display,
                "key": json_key,
                "toggle": {"active": "On", "inactive": "Off"},
                "layers": [],
            }
            layers_doc.setdefault("groups", []).append(group)
            groups_by_key[json_key] = group
        elif adopter_group_key in group_names:
            group["name"] = group_names[adopter_group_key]
        group.setdefault("layers", []).append(build_extra_map_layer(entry, json_key))


def build_batch_layers(extra_layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    batch_layers: list[dict[str, Any]] = []
    for entry in extra_layers:
        item: dict[str, Any] = {
            "source-table": entry["source_table"],
            "primary-key": entry["primary_key"],
            "area-of-interest-id-column": entry["area_of_interest_id_column"],
            "creation-date-column": entry["created_at_column"],
            "geometry-column": entry["geometry_column"],
            "layer-name": entry["layer_name"],
            "srid": entry["srid"],
            "where-clause": entry["where_clause"],
        }
        if entry.get("updated_at_column"):
            item["updated-at-column"] = entry["updated_at_column"]
        if entry.get("label_column"):
            item["label-column"] = entry["label_column"]
        extras = entry.get("additional_columns") or []
        if extras:
            item["additional-columns"] = extras
        batch_layers.append(item)
    return batch_layers

def apply_screen_text_overrides(
    installation: dict[str, Any],
    screens_yaml: dict[str, Any],
    aoi_detail_fields: list[dict[str, str]],
) -> None:
    """Apply localized screen labels from adopter-config to installation-config."""
    home = installation["screens"]["home"]
    downloads = installation["screens"]["downloads"]

    identifier = screens_yaml.get("identifier")
    if isinstance(identifier, dict):
        target = home.get("identifier")
        if isinstance(target, dict):
            if "label" in identifier:
                target["label"] = identifier["label"]
            if "placeholder" in identifier:
                target["placeholder"] = identifier["placeholder"]

    target = home.setdefault("detail", {})
    detail = screens_yaml.get("detail")
    if isinstance(detail, dict):
        for yaml_key, json_key in (
            ("section_title", "sectionTitle"),
            ("area_of_interest_section_title", "areaOfInterestSectionTitle"),
            ("registration_date_label", "registrationDateLabel"),
            ("alteration_date_label", "alterationDateLabel"),
            ("latitude_label", "latitudeLabel"),
            ("longitude_label", "longitudeLabel"),
            ("area_label", "areaLabel"),
            ("features_download_label", "featuresDownloadLabel"),
        ):
            if yaml_key in detail:
                target[json_key] = detail[yaml_key]
    target["fields"] = aoi_detail_fields

    downloads_yaml = screens_yaml.get("downloads")
    if isinstance(downloads_yaml, dict):
        theme = downloads_yaml.get("theme")
        if isinstance(theme, dict):
            target = downloads.get("theme")
            if isinstance(target, dict):
                if "label" in theme:
                    target["label"] = theme["label"]
                if "placeholder" in theme:
                    target["placeholder"] = theme["placeholder"]
        for yaml_key, json_key in (
            ("level1_section_title", "level1SectionTitle"),
            ("level2_section_title", "level2SectionTitle"),
            ("filter_by_title", "filterByTitle"),
        ):
            if yaml_key in downloads_yaml:
                downloads[json_key] = downloads_yaml[yaml_key]


def ask_screen_text_fields(screens: dict[str, Any]) -> None:
    """Collect localized home and downloads screen labels in the wizard."""
    identifier = screens.setdefault("identifier", {})
    identifier["label"] = ask_field(
        "Home identifier label", identifier.get("label", "Identifier"),
        "Label for the registration ID search field on the home screen.",
        "the home screen search form",
    )
    identifier["placeholder"] = ask_field(
        "Home identifier placeholder", identifier.get("placeholder", "Enter the identifier"),
        "Placeholder shown inside the registration ID search field.",
        "the home screen search form",
    )

    print("\n  --- Home detail labels ---")
    print("  Text shown on the registration detail panel after a search.")
    detail = screens.setdefault("detail", {})
    detail_fields = (
        ("section_title", "Detail section title", "Search details"),
        ("area_of_interest_section_title","Detail area of interest section title","Area of interest data"),
        ("registration_date_label", "Registration date label", "Registration date"),
        ("alteration_date_label", "Alteration date label", "Alteration date"),
        ("latitude_label", "Latitude label", "Latitude"),
        ("longitude_label", "Longitude label", "Longitude"),
        ("area_label", "Area label", "Area"),
        ("features_download_label", "Features download label", "Download features"),
    )
    for key, label, default in detail_fields:
        detail[key] = ask_field(
            label, detail.get(key, default),
            f"Label for '{default}' on the registration detail panel.",
            "the home screen detail panel",
        )

    print("\n  --- Downloads screen labels ---")
    print("  Text shown on filters and section headers in the downloads screen.")
    downloads = screens.setdefault("downloads", {})
    theme = downloads.setdefault("theme", {})
    theme["label"] = ask_field(
        "Downloads theme label", theme.get("label", "Theme"),
        "Label for the theme filter on the downloads screen.",
        "the downloads screen",
    )
    theme["placeholder"] = ask_field(
        "Downloads theme placeholder", theme.get("placeholder", "All themes"),
        "Placeholder for the theme filter on the downloads screen.",
        "the downloads screen",
    )
    downloads["level1_section_title"] = ask_field(
        "Downloads level 1 section title",
        downloads.get(
            "level1_section_title",
            "Select the continent you want to access for Downloads",
        ),
        "Introductory text for territorial level 1 on the downloads screen.",
        "the downloads screen",
    )
    downloads["level2_section_title"] = ask_field(
        "Downloads level 2 section title",
        downloads.get("level2_section_title", "Options for the selected continent"),
        "Introductory text for territorial level 2 on the downloads screen.",
        "the downloads screen",
    )
    downloads["filter_by_title"] = ask_field(
        "Downloads filter-by title", downloads.get("filter_by_title", "Filter by:"),
        "Prefix shown before download filters.",
        "the downloads screen",
    )


# Source structure fields. Can be reused as default
# when another layer of the same source_table is configured in this wizard.
SOURCE_STRUCTURE_FIELDS = (
    "parent_key",
    "primary_key",
    "created_at_column",
    "updated_at_column",
    "label_column",
    "geometry_column",
    "additional_columns",
    "srid",
)


def snapshot_source_structure(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy only the source structure fields (wizard memory)."""
    snapshot: dict[str, Any] = {}
    for field in SOURCE_STRUCTURE_FIELDS:
        if field in entry:
            snapshot[field] = copy.deepcopy(entry[field])
    return snapshot


def remember_source_structure(
    memory: dict[str, dict[str, Any]],
    entry: dict[str, Any],
) -> None:
    """Remember the last structure mapping for this layer's source_table."""
    source_table = str(entry.get("source_table") or "").strip()
    if not source_table:
        return
    memory[source_table] = snapshot_source_structure(entry)


def lookup_source_structure(
    memory: dict[str, dict[str, Any]] | None,
    source_table: str,
) -> dict[str, Any]:
    """Return the last mapping for this source in this execution, or empty."""
    if not memory:
        return {}
    previous = memory.get(source_table)
    return copy.deepcopy(previous) if previous else {}


def apply_source_structure_defaults(
    entry: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    """Fill empty structure fields with the last mapping from the source."""
    if not previous:
        return
    for field in SOURCE_STRUCTURE_FIELDS:
        current = entry.get(field)
        if current not in (None, ""):
            continue
        if field not in previous:
            continue
        entry[field] = copy.deepcopy(previous[field])


def ask_generic_layer_entry(
    config: dict[str, Any],
    entry: dict[str, Any] | None,
    source_structure_by_table: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask every field of one generic layer (migration + map presentation)."""
    entry = copy.deepcopy(entry) if entry else {}

    while True:
        default_source = entry.get("source_table") or "public.my_layer"
        source_table = str(
            ask_field(
                "source_table", default_source,
                etl_field_help("source_table"),
                "the migration job mapping for this layer",
            )
        ).strip()
        if "<" in source_table:
            print("\n  Enter the origin table as schema.table (e.g. public.my_layer).")
            continue
        try:
            split_schema_table(source_table)
            validate_source_table_schema(source_table, "etl.layers")
        except ValueError as exc:
            print(f"\n  {exc}")
            continue
        break
    entry["source_table"] = source_table
    apply_source_structure_defaults(
        entry,
        lookup_source_structure(source_structure_by_table, source_table),
    )

    for field in (
        "primary_key",
        "parent_key",
        "label_column",
        "geometry_column",
        "created_at_column",
        "updated_at_column",
    ):
        field_help = (
            etl_field_help("layer_parent_key")
            if field == "parent_key"
            else etl_field_help(field)
        )
        if field in ("updated_at_column", "label_column"):
            while True:
                value = ask_optional_field(
                    field,
                    entry.get(field) or "",
                    field_help,
                    "the migration job mapping for this layer",
                )
                if value is None or ("<" not in value):
                    entry[field] = value
                    break
                print("\n  Enter the source column name.")
            continue
        while True:
            value = str(
                ask_field(
                    field,
                    entry.get(field) or "",
                    field_help,
                    "the migration job mapping for this layer",
                )
            ).strip()
            if value and "<" not in value:
                entry[field] = value
                break
            print("\n  Enter the source column name.")

    entry["additional_columns"] = ask_layer_additional_columns(entry)
    entry["where_clause"] = ask_field(
        "where_clause", entry.get("where_clause", "1=1"),
        "Optional SQL filter applied while reading this layer.",
        "the ETL source query",
    )

    default_layer_name = entry.get("layer_name") or table_name_from_source(source_table)
    while True:
        raw_name = str(
            ask_field(
                "Layer name", default_layer_name,
                "Technical WMS id (lowercase, digits, hyphens, underscores). "
                "Published as dsp:<name>; physical table is dsp.<name> with hyphens as underscores.",
                "GeoServer and the map layer selector",
            )
        ).strip()
        try:
            entry["layer_name"] = validate_layer_name(raw_name, "layer_name")
            break
        except ValueError as exc:
            print(f"\n  {exc}")

    entry["srid"] = ask_int_field(
        "Layer SRID", entry.get("srid", 4326),
        "Coordinate reference system identifier of the source geometry.",
        "ETL geometry conversion and GeoServer layers",
        minimum=1,
    )

    entry["display_name"] = ask_field(
        "Display name", entry.get("display_name") or entry["layer_name"],
        "Human-readable label shown in the map panel (any language).",
        "the map layer selector",
    )

    group_names = config["map"]["group_names"]
    existing_keys = sorted(group_names)
    default_group_key = entry.get("group_key") or (existing_keys[0] if existing_keys else "thematic")
    keys_list = ", ".join(existing_keys) if existing_keys else "(none yet)"
    group_key_help = (
        "Stable group id. Enter an existing key to add this layer to that group, "
        "or type a new key to create a group (you will be asked for its display name next). "
        f"Existing keys: {keys_list}."
    )
    if existing_keys:
        print(
            f"\n  Example: reuse `{existing_keys[0]}` or create `environmental_layers`."
        )
    group_key = str(
        ask_field(
            "Map group key", default_group_key,
            group_key_help,
            "the map layer selector",
        )
    ).strip()
    entry["group_key"] = group_key
    if group_key not in group_names:
        group_names[group_key] = ask_field(
            f"Map group name — {group_key}", group_key.replace("_", " ").capitalize(),
            "Display title for the new group (only asked when the key is new).",
            "the map layer selector",
        )

    entry["active_default"] = ask_bool_field(
        "Enable this layer by default", bool(entry.get("active_default", False)),
        "Whether the layer starts enabled when the map opens.",
        "the initial map state",
    )
    entry["color"] = ask_color_field(
        "Stroke color", entry.get("color", "#2563EB"),
        "Line color used to draw the layer.",
        "the map and generated GeoServer style",
    )
    entry["fill_color"] = ask_color_field(
        "Fill color", entry.get("fill_color", "transparent"),
        "Fill color used inside the layer; use transparent when needed.",
        "the map and generated GeoServer style",
        allow_transparent=True,
    )
    return entry


def ask_generic_layers(config: dict[str, Any]) -> None:
    """Review, edit, and add generic layers under etl.layers."""
    etl = config["etl"]
    declared = list(etl.get("layers") or [])
    result: list[dict[str, Any]] = []
    source_structure_by_table: dict[str, dict[str, Any]] = {}

    print("\n  Generic layers")
    print("  What: extra source tables migrated to dsp.<layer_name> and published as WMS layers.")
    print("  Used in: the migration job, GeoServer publishing, and the map layer selector")

    for item in declared:
        if not isinstance(item, dict):
            continue
        print(f"\n  Declared layer: {item.get('source_table', '<unset>')}")
        if not ask_bool("  Keep this layer", True):
            continue
        if ask_bool("  Edit this layer", False):
            item = ask_generic_layer_entry(config, item, source_structure_by_table)
        else:
            blocked = layer_canonical_source_columns(item)
            try:
                extras = parse_column_list(
                    item.get("additional_columns") or [], "additional_columns"
                )
                needs_fix = bool(columns_clashing_with(extras, blocked)) or bool(
                    columns_clashing_with(
                        extras, LAYER_CANONICAL_TARGET_COLUMNS, ignore_case=True
                    )
                )
            except ValueError:
                needs_fix = True
            if needs_fix:
                print(
                    "\n  additional_columns lists columns already mapped or reserved "
                    "on the destination for this layer."
                )
                print(f"  Already mapped: {', '.join(blocked)}")
                print(
                    "  Reserved destination: "
                    f"{', '.join(LAYER_CANONICAL_TARGET_COLUMNS)}"
                )
                item = copy.deepcopy(item)
                item["additional_columns"] = ask_layer_additional_columns(item)
        result.append(item)
        remember_source_structure(source_structure_by_table, item)

    add_next = not result
    while ask_bool("\n  Add a generic layer" if not result else "\n  Add another generic layer", add_next):
        item = ask_generic_layer_entry(config, None, source_structure_by_table)
        result.append(item)
        remember_source_structure(source_structure_by_table, item)
        add_next = False

    etl["layers"] = result


def config_display_path(active: Path, root: Path | None = None) -> str:
    if root is not None:
        try:
            return str(active.resolve().relative_to(root.resolve()))
        except ValueError:
            pass
    return str(active)


def ask_existing_config_file(active: Path, config_ref: str) -> bool:
    print("\nHow do you want to configure the adopter?")
    print("  1. Guided configuration")
    print("     Create adopter-config.yaml step by step.")
    print("  2. Existing configuration")
    print("     Use an existing adopter-config.yaml file.")

    while True:
        choice = ask("Choice", "1")
        if choice == "1":
            print("\nThe wizard will save your answers when you finish.")
            print(
                "You can edit that YAML manually at any time; run ./config.sh "
                "and choose 1 (reapply) to regenerate the operational files."
            )
            return True
        if choice == "2":
            print(f"\nPlace your adopter-config.yaml at:\n  {config_ref}")
            print("You can edit that file manually whenever you need.")
            print("When ready, run ./config.sh and choose 1 (reapply).")
            return False
        print("Invalid choice. Enter 1 or 2.")


def wizard(example: Path, active: Path, *, edit: bool = False, root: Path | None = None) -> bool:
    print()
    config_ref = config_display_path(active, root)
    template = yaml.safe_load(example.read_text(encoding="utf-8"))
    if edit:
        if not active.is_file():
            print(f"Error: file not found: {active}", file=sys.stderr)
            raise SystemExit(1)
        saved = yaml.safe_load(active.read_text(encoding="utf-8"))
        config = deep_merge(template, saved)
        print("Edit adopter configuration")
        print("Each stage explains the field and where its value is used.")
        print("Values between brackets are the current saved values.")
        print("Press Enter to keep the displayed value.")
    else:
        config = copy.deepcopy(template)
        print("Each stage explains the field and where its value is used.")
        print("Values between brackets are default/example values.")
        print("Press Enter to accept the displayed default/example value.")
        if not ask_existing_config_file(active, config_ref):
            return False

    print("\n" + "=" * 72)
    print("Stage 1/4 — Source database and spatial reference")
    print("=" * 72)
    env = config["environment"]
    env["source_jdbc_url"] = ask_field(
        "Source JDBC URL", env["source_jdbc_url"],
        "JDBC address of the database that contains the adopter data.",
        "the ETL migration job and .env",
    )
    env["source_db_user"] = ask_field(
        "Source database user", env["source_db_user"],
        "User used to read the source database.",
        "the ETL migration job and .env",
    )
    env["source_db_password"] = ask_field(
        "Source database password", env["source_db_password"],
        "Password used to read the source database.",
        "the ETL migration job and .env",
    )
    for key, label in (
        ("territory_level_1", "Territorial level 1 SRID"),
        ("territory_level_2", "Territorial level 2 SRID"),
        ("territory_level_3", "Territorial level 3 SRID"),
        ("area_of_interest", "Area of interest SRID"),
    ):
        env["layer_srs"][key] = ask_int_field(
            label, env["layer_srs"][key],
            "Coordinate reference system identifier of the source geometry.",
            "ETL geometry conversion and GeoServer layers",
            minimum=1,
        )

    print("\n" + "=" * 72)
    print("Stage 2/4 — Source tables, columns, and layers")
    print("=" * 72)
    theme_count = ask_int_field(
        "Number of theme KPIs (0-4)", config["installation"]["kpis"]["theme_count"],
        "Number of optional theme measurements available in the source data. "
        "The Area of Interest (AOI) KPI is always present — this value only "
        "controls additional theme KPIs (use 0 when there are no theme columns).",
        "generated KPI cards and ETL theme mappings",
        minimum=0,
        maximum=4,
    )
    config["installation"]["kpis"]["theme_count"] = theme_count
    reset_disabled_themes(config, template, theme_count)
    for name, fields in (
        ("level1", ("source_table", "primary_key", "name_column", "geometry_column", "created_at_column", "updated_at_column")),
        (
            "level2",
            ("source_table", "primary_key", "parent_key", "name_column", "geometry_column", "created_at_column", "updated_at_column"),
        ),
        (
            "level3",
            ("source_table", "primary_key", "parent_key", "name_column", "geometry_column", "created_at_column", "updated_at_column"),
        ),
        (
            "area_of_interest",
            (
                "source_table",
                "primary_key",
                "territory_level_3_column",
                "geometry_column",
                "created_at_column",
                "updated_at_column",
                "area_column",
            ),
        ),
    ):
        print(f"\nEntity: {name}")
        for field in fields:
            section = config["etl"][name]
            if field == "updated_at_column":
                section[field] = ask_optional_field(
                    field,
                    section.get(field),
                    etl_field_help(field),
                    "the ETL job mapping for this entity",
                )
                continue
            section[field] = ask_field(
                field, section[field],
                etl_field_help(field),
                "the ETL job mapping for this entity",
            )
        if name == "area_of_interest":
            aoi_section = config["etl"][name]
            theme_names = expected_theme_source_columns(theme_count)
            additional_blocked = set(aoi_canonical_source_columns(aoi_section)) | set(theme_names)
            aoi_section["additional_columns"] = ask_unblocked_column_list(
                "Extra columns to migrate",
                aoi_section.get("additional_columns") or [],
                etl_field_help("additional_columns"),
                "the ETL job mapping for this entity",
                additional_blocked,
                "etl.area_of_interest.additional_columns",
                target_blocked=AOI_CANONICAL_TARGET_COLUMNS,
            )
            if theme_count > 0:
                business_blocked = set(aoi_canonical_source_columns(aoi_section)) | set(
                    aoi_section["additional_columns"]
                )
                while True:
                    selected = ask_optional_column_list(
                        "Theme KPI columns (business_only_persist_columns)",
                        aoi_section.get("business_only_persist_columns") or theme_names,
                        etl_field_help("business_only_persist_columns"),
                        "the ETL job mapping for this entity",
                    )
                    try:
                        validated = validate_business_only_persist_columns(
                            selected,
                            theme_count,
                            "etl.area_of_interest.business_only_persist_columns",
                        )
                        reject_source_and_target_clashes(
                            validated,
                            business_blocked,
                            AOI_CANONICAL_TARGET_COLUMNS,
                            "etl.area_of_interest.business_only_persist_columns",
                            source_reason="duplicates a required or additional column mapping.",
                        )
                        aoi_section["business_only_persist_columns"] = validated
                        break
                    except ValueError as exc:
                        print(f"\n  {exc}")
            else:
                aoi_section["business_only_persist_columns"] = []
        config["etl"][name]["where_clause"] = ask_field(
            "where_clause", config["etl"][name]["where_clause"],
            "Optional SQL filter applied while reading this entity.",
            "the ETL source query",
        )

    ask_generic_layers(config)
    print(
        "\n  Note: configured generic layers are migrated and published as download themes "
        "in downloadThemesConfig.json."
    )

    print("\n" + "=" * 72)
    print("Stage 3/4 — Application settings")
    print("=" * 72)
    for code in ("area_of_interest", "theme_1", "theme_2", "theme_3", "theme_4"):
        card = config["installation"]["kpis"][code]
        if code != "area_of_interest" and not card["enabled"]:
            continue
        card["label"] = ask_field(
            f"KPI label for {code}", card["label"],
            "Human-readable name shown on the KPI card.", "the application dashboard",
        )
        card["unit_of_measurement"] = ask_field(
            f"KPI unit for {code}", card["unit_of_measurement"],
            "Unit displayed beside the KPI value.", "the application dashboard",
        )
        if code == "area_of_interest":
            card["optional_label"] = ask_field(
                "KPI optional label for area_of_interest", card["optional_label"],
                "Secondary unit shown for the area sum (e.g. ha).",
                "the AREA_OF_INTEREST KPI card",
            )
    area = config["installation"]["area"]
    area["unit"] = ask_field(
        "Area unit code", area["unit"],
        "Short code for the area measurement (e.g. ha, m²).",
        "area labels and KPI subtotals",
    )
    area["unit_label"] = ask_field(
        "Area unit label", area["unit_label"],
        "Label shown beside area values in the UI.",
        "detail screens and downloads",
    )
    formats = config["installation"]["formats"]
    formats["date"] = ask_field(
        "Date format", formats["date"],
        "Pattern for dates without time (Java-style, e.g. dd/MM/yyyy).",
        "lists and detail screens",
    )
    formats["date_time"] = ask_field(
        "Date-time format", formats["date_time"],
        "Pattern for dates with time (e.g. dd/MM/yyyy HH:mm).",
        "detail screens",
    )
    ask_object_storage(config)

    print("\n" + "=" * 72)
    print("Stage 4/4 — Interface")
    print("=" * 72)
    for level in ("level1", "level2", "level3"):
        section = config["installation"]["hierarchy"][level]
        section["label"] = ask_field(
            f"Displayed name for {level}", section["label"],
            "Name shown to users for this hierarchy level.",
            "filters, downloads, and detail screens",
        )
        section["placeholder"] = ask_field(
            f"Selection text for {level}", section["placeholder"],
            "Text shown when the user has not selected this level.",
            "hierarchy filter controls",
        )
    screens = config["installation"]["screens"]
    screens["home_title"] = ask_field(
        "Home screen title", screens["home_title"],
        "Title displayed above the registration search screen.", "the home screen",
    )
    screens["downloads_title"] = ask_field(
        "Downloads screen title", screens["downloads_title"],
        "Title displayed above the public downloads screen.", "the downloads screen",
    )
    ask_screen_text_fields(screens)
    ask_aoi_detail_fields(config)
    ask_kpi_accent_colors(config, theme_count)
    ask_map_initial_view(config)
    group_names = config["map"]["group_names"]
    group_names["territorial_division"] = ask_field(
        "Map group name — territorial division", group_names["territorial_division"],
        "Title of the territorial hierarchy layer group in the map.",
        "the map layer selector",
    )
    group_names["areas_of_interest"] = ask_field(
        "Map group name — areas of interest", group_names["areas_of_interest"],
        "Title of the declared areas layer group in the map.",
        "the map layer selector",
    )
    sync_map_layer_names(config)
    for layer_name, layer in config["map"]["layers"].items():
        print(f"\n  Layer name for {layer_name}")
        print("  What: Same display name defined earlier in this wizard.")
        print("  Used in: the map layer selector")
        print(f"  Value: {layer['name']}")
        print(f"\n  WMS URL for {layer_name}")
        print("  What: Fixed GeoServer WMS endpoint serving this layer.")
        print("  Used in: the map client and GeoServer requests")
        print(f"  Value: {FIXED_WMS_BASE_URL}")
        layer["active_default"] = ask_bool_field(
            f"Enable {layer_name} by default", layer["active_default"],
            "Whether the layer starts enabled when the map opens.",
            "the initial map state",
        )
        layer["color"] = ask_color_field(
            f"Stroke color for {layer_name}", layer["color"],
            "Line color used to draw the layer boundary.",
            "the map and generated GeoServer style",
        )
        layer["fill_color"] = ask_color_field(
            f"Fill color for {layer_name}", layer["fill_color"],
            "Fill color used inside the layer; use transparent when needed.",
            "the map and generated GeoServer style",
            allow_transparent=True,
        )

    ask_about_page(config, example.parent.parent / "about")

    etl = config.get("etl")
    if isinstance(etl, dict):
        etl.pop("jobs", None)

    write_adopter_config(active, config, template)
    print(f"\nConfiguration saved to {active}")
    return True


def ask_object_storage(config: dict[str, Any]) -> None:
    """Collects the object storage credentials for the pre-generated download files.

    The whole block is optional: without an endpoint the pre-generation job stays off and
    downloads keep going straight to the WFS, which is the behaviour before this feature.
    """
    storage = config["environment"].setdefault(
        "object_storage", copy.deepcopy(OBJECT_STORAGE_DEFAULTS)
    )
    print("\nObject storage for pre-generated download files (optional)")
    print("  Leave the endpoint empty to keep serving downloads from the WFS only.")
    storage["endpoint"] = ask_optional_field(
        "Object storage endpoint (S3 API)",
        storage.get("endpoint") or "",
        "Address of the S3-compatible API. Empty disables the pre-generation job.",
        "the geo file generation job and the backend",
    ) or ""
    if not storage["endpoint"]:
        print("  Pre-generation disabled: downloads keep using the WFS.")
        return
    storage["bucket"] = ask_field(
        "Bucket", storage.get("bucket") or OBJECT_STORAGE_DEFAULTS["bucket"],
        "Bucket where the files are published. It must already exist — the job never creates it.",
        "the geo file generation job and the backend",
    )
    storage["region"] = ask_field(
        "Region", storage.get("region") or OBJECT_STORAGE_DEFAULTS["region"],
        "Region reported to the S3 API. Endpoints that ignore it accept any value.",
        "the geo file generation job and the backend",
    )
    storage["access_key"] = ask_field(
        "Access key", storage.get("access_key") or "",
        "Credential with write access for the job and read access for the backend.",
        "the geo file generation job, the backend and .env",
    )
    storage["secret_key"] = ask_field(
        "Secret key", storage.get("secret_key") or "",
        "Secret paired with the access key.",
        "the geo file generation job, the backend and .env",
    )
    storage["path_style_access"] = ask_bool_field(
        "Path-style access",
        bool(storage.get("path_style_access", True)),
        "Keeps the bucket in the path instead of the host name. Endpoints that are not "
        "AWS usually need it on.",
        "the S3 client of the job and the backend",
    )
    while True:
        cron = ask_field(
            "Pre-generation cron",
            storage.get("generation_cron") or OBJECT_STORAGE_DEFAULTS["generation_cron"],
            "Schedule of the geo file job, in a window after the migration (5 fields).",
            "the dsp-job-geo-file-generation service",
        )
        if len(str(cron).split()) == 5:
            storage["generation_cron"] = str(cron)
            break
        print("\n  The cron needs 5 fields (minute hour day month weekday).")


def ask_about_page(config: dict[str, Any], about_dir: Path) -> None:
    """Configure the optional custom About page (about.enabled + tabs)."""
    about = config.setdefault("about", {})
    about_dir = about_dir.resolve()
    print("\n" + "=" * 72)
    print("Optional — Custom About page")
    print("=" * 72)
    print("  What: Replaces the built-in About page with tabs rendered from")
    print("        Markdown files. Paths outside the project are copied into")
    print(f"        {about_dir}")
    print("  Used in: the frontend About page")
    enabled = ask_bool(
        "Enable a custom About page", bool(about.get("enabled", False))
    )
    about["enabled"] = enabled
    if not enabled:
        return

    about["banner_title"] = ask_field(
        "About page — banner title",
        about.get("banner_title", "About"),
        "Title shown at the top of the About page.",
        "the frontend About page",
    )

    tab_count = ask_int_field(
        "Number of About tabs",
        len(about.get("tabs") or []) or 1,
        "How many tabs the About page will show.",
        "the frontend About page",
        minimum=1,
    )

    existing_tabs = about.get("tabs") or []
    tabs: list[dict[str, str]] = []
    for index in range(tab_count):
        existing = existing_tabs[index] if index < len(existing_tabs) else {}
        default_label = existing.get("label") or f"Tab {index + 1}"
        existing_file = str(existing.get("file") or "").strip()
        default_path = ""
        if existing_file:
            candidate = about_dir / existing_file
            if candidate.is_file():
                default_path = str(candidate)

        while True:
            label = str(
                ask_field(
                    f"About tab {index + 1} — label",
                    default_label,
                    "Text shown on the tab.",
                    "the frontend About page",
                )
            ).strip()
            if label:
                break
            print("\n  Enter a non-empty tab label.")

        fallback = f"tab-{index + 1}"
        while True:
            raw_path = str(
                ask_field(
                    f"About tab {index + 1} — Markdown file",
                    default_path,
                    "Path to a .md or .markdown file on this computer. "
                    f"Files outside {about_dir} are copied there.",
                    "the frontend About page",
                )
            ).strip()
            if not raw_path or "<" in raw_path:
                print("\n  Enter a path to a Markdown file (e.g. ~/docs/overview.md).")
                continue
            source = resolve_about_source_path(raw_path)
            if not source.is_file():
                print(f"\n  File not found: {source}")
                continue
            if source.suffix.lower() not in ABOUT_MARKDOWN_SUFFIXES:
                print("\n  The file must have a .md or .markdown extension.")
                continue
            dest_name = about_tab_destination(label, source, about_dir, fallback)
            if not is_safe_about_filename(dest_name):
                print("\n  The destination file name is not allowed.")
                continue
            dest = about_dir / dest_name
            if is_inside_about_dir(source, about_dir):
                tabs.append({"label": label, "file": dest_name})
                break
            if dest.exists() and dest.resolve() != source.resolve():
                if not ask_bool(f"Overwrite existing file {dest.name}", False):
                    print("\n  Choose a different Markdown file.")
                    continue
            try:
                about_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            except OSError as exc:
                print(f"\n  Could not copy the file: {exc}")
                continue
            tabs.append({"label": label, "file": dest_name})
            break

    about["tabs"] = tabs


def replace_env(env_file: Path, values: dict[str, Any]) -> None:
    if not env_file.exists():
        example = env_file.with_name(".env.example")
        shutil.copyfile(example, env_file)
    lines = env_file.read_text(encoding="utf-8").splitlines()
    replacements = {
        "DSP_SOURCE_JDBC_URL": get(values, "environment", "source_jdbc_url"),
        "DSP_SOURCE_DB_USER": get(values, "environment", "source_db_user"),
        "DSP_SOURCE_DB_PASSWORD": get(values, "environment", "source_db_password"),
    }
    srs = get(values, "environment", "layer_srs", default={})
    for name, value in srs.items():
        replacements[f"LAYER_SRS_{name.upper()}"] = value
    storage = object_storage_settings(values)
    replacements.update(
        {
            "DSP_OBJECT_STORAGE_ENDPOINT": storage["endpoint"],
            "DSP_OBJECT_STORAGE_REGION": storage["region"],
            "DSP_OBJECT_STORAGE_BUCKET": storage["bucket"],
            "DSP_OBJECT_STORAGE_ACCESS_KEY": storage["access_key"],
            "DSP_OBJECT_STORAGE_SECRET_KEY": storage["secret_key"],
            "DSP_OBJECT_STORAGE_PATH_STYLE_ACCESS": str(
                bool(storage["path_style_access"])
            ).lower(),
            # Quoted because the value carries spaces, like DSP_MIGRATION_CRON.
            "DSP_GEO_FILE_GENERATION_CRON": f"\"{storage['generation_cron']}\"",
        }
    )
    result = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in replacements:
            result.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            result.append(line)
    for key, value in replacements.items():
        if key not in seen:
            result.append(f"{key}={value}")
    env_file.write_text("\n".join(result) + "\n", encoding="utf-8")


def set_source_mapping(section: dict[str, Any], values: dict[str, Any]) -> None:
    primary_key = require_simple_primary_key(values.get("primary_key"), "primary-key")
    geometry_column = require_non_blank_column(values.get("geometry_column"), "geometry-column")
    creation_date_column = require_non_blank_column(
        values.get("created_at_column"), "created-at-column"
    )
    updated_at_column = resolve_optional_column(
        values.get("updated_at_column"), "updated-at-column"
    )
    name_column = values.get("name_column")
    if isinstance(name_column, str):
        name_column = name_column.strip() or None
    else:
        name_column = None
    parent_key = values.get("parent_key")
    if isinstance(parent_key, str):
        parent_key = parent_key.strip() or None
    else:
        parent_key = None

    section["source-table"] = values["source_table"]
    section["primary-key"] = primary_key
    section["geometry-column"] = geometry_column
    section["creation-date-column"] = creation_date_column
    if updated_at_column:
        section["updated-at-column"] = updated_at_column
    else:
        section.pop("updated-at-column", None)
    section["where-clause"] = values.get("where_clause") or "1=1"
    if parent_key:
        section["partition-column"] = parent_key
    else:
        section.pop("partition-column", None)

    persist = [primary_key, creation_date_column]
    if name_column:
        persist.append(name_column)
    if parent_key:
        persist.append(parent_key)
    if updated_at_column:
        persist.append(updated_at_column)
    section["persist-columns"] = persist

    mapping = {
        primary_key: "id",
        geometry_column: "geom",
        creation_date_column: "created_at",
    }
    if name_column:
        mapping[name_column] = "name"
    if parent_key:
        mapping[parent_key] = "parent_id"
    if updated_at_column:
        mapping[updated_at_column] = "updated_at"
    section["column-mapping"] = mapping
    section.pop("comparison-columns", None)
    section.pop("change-detection-strategy", None)


def object_storage_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Reads environment.object_storage, filling in what the adopter did not declare."""
    settings = dict(OBJECT_STORAGE_DEFAULTS)
    declared = get(values, "environment", "object_storage", default={})
    if isinstance(declared, dict):
        for key in settings:
            value = declared.get(key)
            if value is not None and value != "":
                settings[key] = value
    settings["endpoint"] = str(settings["endpoint"]).strip()
    settings["path_style_access"] = bool(settings["path_style_access"])
    cron = str(settings["generation_cron"]).strip()
    if len(cron.split()) != 5:
        raise ValueError(
            "environment.object_storage.generation_cron must have 5 fields "
            "(minute hour day month weekday)."
        )
    settings["generation_cron"] = cron
    if settings["endpoint"] and not (settings["access_key"] and settings["secret_key"]):
        raise ValueError(
            "environment.object_storage needs access_key and secret_key when endpoint is set. "
            "Clear the endpoint to keep downloads on the WFS only."
        )
    return settings


def validate_job_migration_path(root: Path) -> None:
    raw = read_dotenv_value(
        root / ".env",
        "DSP_JOB_MIGRATION_PATH",
        default="../rer-dsp-job-data-migration",
    )
    path = Path(str(raw))
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()
    dockerfile = path / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError(
            f"Migration job repository not found at: {path} "
            f"(expected Dockerfile). Clone rer-dsp-job-data-migration "
            f"or set DSP_JOB_MIGRATION_PATH in .env."
        )


def write_geo_file_generation_config(root: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Generates the runtime YAML of the pre-generation job from the adopter values.

    The connection details also go to .env, because Compose passes them to the backend,
    which reads the same bucket.
    """
    storage = object_storage_settings(values)
    example = root / "config/Job-Geo-File-Generation/application/application.yaml.example"
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    document["dsp"]["object-storage"].update(
        {
            "endpoint": storage["endpoint"],
            "region": storage["region"],
            "bucket": storage["bucket"],
            "access-key": storage["access_key"],
            "secret-key": storage["secret_key"],
            "path-style-access": storage["path_style_access"],
        }
    )
    # No endpoint means no storage to publish to: the job runs and does nothing, which
    # is noisier than not running it.
    document["execution-jobs"]["geo-file-generation-job"] = bool(storage["endpoint"])
    output = root / "config/Job-Geo-File-Generation/application/application.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dump_yaml(document), encoding="utf-8")
    return storage


def apply_config(root: Path, active: Path, *, quiet: bool = False) -> None:
    example = root / "config/adopter/adopter-config.yaml.example"
    template = yaml.safe_load(example.read_text(encoding="utf-8"))
    values = yaml.safe_load(active.read_text(encoding="utf-8"))
    validate_job_migration_path(root)
    theme_count = get(values, "installation", "kpis", "theme_count", default=4)
    if not isinstance(theme_count, int) or theme_count < 0 or theme_count > 4:
        raise ValueError("theme_count must be an integer between 0 and 4.")
    config_changed = reset_disabled_themes(values, template, theme_count)
    config_changed = sync_map_layer_names(values) or config_changed
    etl = values.get("etl")
    if isinstance(etl, dict) and etl.pop("jobs", None) is not None:
        config_changed = True
    if config_changed:
        write_adopter_config(active, values, template)
    source_values = []
    for entity in ("level1", "level2", "level3", "area_of_interest"):
        entity_values = get(values, "etl", entity, default={})
        if not isinstance(entity_values, dict):
            continue
        for key, value in entity_values.items():
            if key in {"additional_columns", "business_only_persist_columns"}:
                continue
            source_values.append(value)
    invalid = [
        str(value)
        for value in source_values
        if isinstance(value, str) and ("<source_" in value or value == "source_schema.source_table")
    ]
    if invalid:
        raise ValueError(
            "The ETL configuration still contains placeholders. "
            "Fill in the source table and columns in the wizard or adopter-config.yaml."
        )
    for layer_name, layer in get(values, "map", "layers", default={}).items():
        color = layer.get("color")
        fill_color = layer.get("fill_color")
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise ValueError(
                f"map.layers.{layer_name}.color must be a #RGB or #RRGGBB value."
            )
        if not isinstance(fill_color, str) or (
            fill_color != "transparent" and not HEX_COLOR.fullmatch(fill_color)
        ):
            raise ValueError(
                f"map.layers.{layer_name}.fill_color must be 'transparent' or a #RGB/#RRGGBB value."
            )
    extra_layers = validate_extra_layers(values)
    additional_columns = aoi_additional_columns(values)
    business_only_columns = aoi_business_only_persist_columns(values, theme_count)
    aoi_detail_fields = validate_aoi_detail_fields(values, additional_columns)
    if coerce_map_initial_view_to_planet(values):
        write_adopter_config(active, values, template)
    map_initial_view = validate_map_initial_view(values)
    kpis = get(values, "installation", "kpis", default={})
    for code in enabled_kpi_codes(theme_count):
        accent_color = get(kpis, code, "accent_color")
        if not isinstance(accent_color, str) or not HEX_COLOR.fullmatch(accent_color):
            raise ValueError(
                f"installation.kpis.{code}.accent_color must be a #RGB or #RRGGBB value."
            )
    write_adopter_config(active, values, template)
    installation = json.loads(
        (root / "config/installation/installation-config.json.example").read_text(
            encoding="utf-8"
        )
    )
    hierarchy = get(values, "installation", "hierarchy", default={})
    for item in installation["hierarchy"]:
        override = hierarchy.get(item["key"], {})
        item["label"] = override.get("label", item["label"])
        item["placeholder"] = override.get("placeholder", item["placeholder"])
    screens = get(values, "installation", "screens", default={})
    installation["screens"]["home"]["title"] = screens.get(
        "home_title", installation["screens"]["home"]["title"]
    )
    installation["screens"]["downloads"]["title"] = screens.get(
        "downloads_title", installation["screens"]["downloads"]["title"]
    )
    apply_screen_text_overrides(installation, screens, aoi_detail_fields)
    cards = get(values, "installation", "kpis", default={})
    configured_cards = []
    for card in installation["kpis"]["cards"]:
        override = cards.get(card["code"].lower(), {})
        if card["code"].startswith("THEME_"):
            theme_number = int(card["code"].split("_")[1])
            if theme_number > theme_count:
                continue
        if override.get("enabled") is False:
            continue
        for source, target in (
            ("label", "label"),
            ("unit_of_measurement", "unitOfMeasurement"),
            ("optional_label", "optionalLabel"),
        ):
            if source in override:
                card[target] = override[source]
        accent_color = override.get("accent_color")
        if isinstance(accent_color, str) and HEX_COLOR.fullmatch(accent_color):
            card["accentColor"] = accent_color
        configured_cards.append(card)
    installation["kpis"]["cards"] = configured_cards
    area = get(values, "installation", "area", default={})
    installation["areaOfInterest"]["areaUnit"] = area.get(
        "unit", installation["areaOfInterest"]["areaUnit"]
    )
    installation["areaOfInterest"]["areaUnitLabel"] = area.get(
        "unit_label", installation["areaOfInterest"]["areaUnitLabel"]
    )
    formats = get(values, "installation", "formats", default={})
    installation["formats"]["date"] = formats.get("date", installation["formats"]["date"])
    installation["formats"]["dateTime"] = formats.get(
        "date_time", installation["formats"]["dateTime"]
    )
    installation["map"] = map_initial_view
    install_file = root / "config/installation/installation-config.json"
    install_file.write_text(
        json.dumps(installation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    layers = json.loads(
        (root / "config/map/mapLayersConfig.json.example").read_text(encoding="utf-8")
    )
    layer_values = get(values, "map", "layers", default={})
    layer_names = {
        "territory-level-1": "level1",
        "territory-level-2": "level2",
        "territory-level-3": "level3",
        "area-of-interest": "area_of_interest",
    }
    for group in layers["groups"]:
        for layer in group["layers"]:
            identifier = layer["layers"].split(":")[-1]
            override = layer_values.get(layer_names[identifier], {})
            for source, target in (
                ("name", "name"),
                ("active_default", "activeDefault"),
            ):
                if source in override:
                    layer[target] = override[source]
                    if target == "activeDefault":
                        layer["active"] = override[source]
            layer.setdefault("style", {})["color"] = override.get(
                "color", layer["style"]["color"]
            )
            layer["style"]["fillColor"] = override.get(
                "fill_color", layer["style"]["fillColor"]
            )
        group_key = "territorial_division" if group["key"] == "dt" else "areas_of_interest"
        if group["key"] in ("dt", "ird"):
            group["name"] = get(values, "map", "group_names", group_key, default=group["name"])
    append_extra_layers_to_map(layers, values, extra_layers)
    map_file = root / "config/map/mapLayersConfig.json"
    map_file.write_text(json.dumps(layers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    download_themes = build_download_themes_config(values, extra_layers)
    download_dir = root / "config/downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    download_file = root / "config/downloads/downloadThemesConfig.json"
    download_file.write_text(
        json.dumps(download_themes, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    about_config = build_about_config(values, root / "config/about")
    about_file = root / "config/about/about-config.json"
    about_file.write_text(
        json.dumps(about_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    migration_example = root / "config/Job-Data-Migration/application/application.yaml.example"
    migration = yaml.safe_load(migration_example.read_text(encoding="utf-8"))
    etl = get(values, "etl", default={})
    for name in ("level1", "level2", "level3"):
        set_source_mapping(migration["batch"]["admin-unit"][name.replace("level", "level-")], etl[name])
    aoi = migration["batch"]["area-of-interest"]
    aoi_values = etl["area_of_interest"]
    updated_at_column = resolve_optional_column(
        aoi_values.get("updated_at_column"),
        "etl.area_of_interest.updated_at_column",
    )
    aoi.update(
        {
            "source-table": aoi_values["source_table"],
            "primary-key": require_simple_primary_key(
                aoi_values.get("primary_key"), "etl.area_of_interest.primary_key"
            ),
            "creation-date-column": require_non_blank_column(
                aoi_values.get("created_at_column"),
                "etl.area_of_interest.created_at_column",
            ),
            "territory-level-3-column": require_non_blank_column(
                aoi_values.get("territory_level_3_column"),
                "etl.area_of_interest.territory_level_3_column",
            ),
            "total-area-column": require_non_blank_column(
                aoi_values.get("area_column"), "etl.area_of_interest.area_column"
            ),
            "geometry-column": require_non_blank_column(
                aoi_values.get("geometry_column"),
                "etl.area_of_interest.geometry_column",
            ),
            "where-clause": aoi_values.get("where_clause") or "1=1",
        }
    )
    if updated_at_column:
        aoi["updated-at-column"] = updated_at_column
    else:
        aoi.pop("updated-at-column", None)
    canonical_source = aoi_canonical_source_columns(aoi_values)
    reject_source_and_target_clashes(
        additional_columns,
        canonical_source,
        AOI_CANONICAL_TARGET_COLUMNS,
        "etl.area_of_interest.additional_columns",
    )
    aoi["additional-columns"] = additional_columns
    aoi.pop("comparison-columns", None)
    aoi.pop("column-mapping", None)
    aoi.pop("change-detection-strategy", None)
    aoi["business-only-persist-columns"] = business_only_columns
    reject_source_and_target_clashes(
        business_only_columns,
        set(canonical_source) | set(additional_columns),
        AOI_CANONICAL_TARGET_COLUMNS,
        "etl.area_of_interest.business_only_persist_columns",
        source_reason="duplicates a required or additional column mapping.",
    )
    migration["batch"]["layers"] = build_batch_layers(extra_layers)
    migration["execution-jobs"]["admin-unit-level-1-geoserver-job"] = True
    migration["execution-jobs"]["admin-unit-level-2-geoserver-job"] = True
    migration["execution-jobs"]["admin-unit-level-3-geoserver-job"] = True
    migration["execution-jobs"]["area-of-interest-geoserver-job"] = True
    layer_jobs_enabled = bool(extra_layers)
    migration["execution-jobs"]["layer-jobs"] = layer_jobs_enabled
    output = root / "config/Job-Data-Migration/application/application.yaml"
    output.write_text(dump_yaml(migration), encoding="utf-8")

    storage = write_geo_file_generation_config(root, values)
    replace_env(root / ".env", values)
    if not quiet:
        print("Configuration files generated successfully.")
        if extra_layers:
            print(f"  Generic layers: {len(extra_layers)}")
        print(f"  Download themes: {len(download_themes.get('themes', []))}")
        if storage["endpoint"]:
            print(
                f"  Pre-generated files: bucket '{storage['bucket']}' "
                f"at {storage['endpoint']} (cron {storage['generation_cron']})"
            )
        else:
            print("  Pre-generated files: disabled (downloads served by the WFS)")
        print("\nNext steps:")
        print(f"  1. Review: {active}")
        print(
            "  2. For SQL subqueries, keep source_table in a YAML folded block (>-)"
        )
        print("  3. Run ./setup.sh and choose Real adopter.")
        print("  4. Run ./start.sh to start the application.")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--wizard", action="store_true")
    parser.add_argument(
        "--edit",
        action="store_true",
        help="Edit an existing configuration (use with --wizard).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress success message and next steps (used by setup.sh).",
    )
    args = parser.parse_args()
    example = args.root / "config/adopter/adopter-config.yaml.example"
    active = args.config or args.root / "config/adopter/adopter-config.yaml"
    if args.edit and not args.wizard:
        print("Error: --edit requires --wizard.", file=sys.stderr)
        raise SystemExit(1)
    if args.wizard:
        try:
            if not wizard(example, active, edit=args.edit, root=args.root):
                return
        except ValueError as error:
            print(f"\nConfiguration error: {error}", file=sys.stderr)
            raise SystemExit(1)
    if not active.exists():
        print(f"Error: file not found: {active}", file=sys.stderr)
        print("Run ./config.sh to start the guided configuration.", file=sys.stderr)
        raise SystemExit(1)
    try:
        apply_config(args.root, active, quiet=args.quiet)
    except (KeyError, TypeError, ValueError) as error:
        print(f"Adopter configuration error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
