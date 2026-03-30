#!/usr/bin/env python3

# Interactive menu for this Pulumi project: stack checklist, seed defaults, set missing config,
# Azure Key Vault preflight/creation (via create_keyvault), peerings/routes/NSG helpers, and on-prem CIDR helper.

# Major pieces: YAML merge (default_vars → Pulumi.<stack>.yaml), Azure "special" config builders,
# stack discovery (pulumi stack ls or local files), and interactive_menu() driving two layouts
# (all stacks complete vs. at least one incomplete).
# Run from the project directory root.  You must login with the Azure CLI.
# Developed by Andrew Tamagni

# Some portions of this script were developed with assistance from Cursor AI. The specific underlying
# model can vary by session and configuration. AI assistance was used for parts of code generation and
# documentation, and all code/documentation have been reviewed, verified, and refined by humans for
# quality and accuracy.

# Usage: python stack_menu.py

import copy
import os
import re
import sys
import json
import subprocess
import ipaddress
import yaml
from typing import NoReturn

import create_keyvault

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# ANSI color codes (disabled when stdout is not a terminal).
# Scheme: green=success, cyan=info, orange=warning, red=error.
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_CYAN = "\033[36m"
COLOR_ORANGE = "\033[33m"
COLOR_RED = "\033[31m"

# Placeholders from default_vars.yaml; __REQUIRED__ keys must be set via pulumi config.
REQUIRED_TOKEN = "__REQUIRED__"
OPTIONAL_TOKEN = "__OPTIONAL__"
SECRET_TOKEN = "__SECRET__"
CONFIG_MISSING = object()


# -----------------------------------------------------------------------------
# Console output helpers
# -----------------------------------------------------------------------------

def color_enabled() -> bool:
    """Return True if we should emit ANSI colors (when stdout is a terminal)."""
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False

def msg(text: str, color_code: str | None = None) -> None:
    """Print message to stdout with optional color. If color disabled or None, print plain."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}")
    else:
        print(text)

def msg_stderr(text: str, color_code: str | None = None) -> None:
    """Print message to stderr with optional color (e.g. for error output)."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}", file=sys.stderr)
    else:
        print(text, file=sys.stderr)

def fail(text: str) -> NoReturn:
    """Print an error message to stderr and exit with status 1."""
    msg_stderr(f"ERROR : {text}", COLOR_RED)
    raise SystemExit(1)

def quit_input_detected(choice: str) -> bool:
    """Return True when user input is any quit token we accept."""
    return choice in ("q", "quit")

# -----------------------------------------------------------------------------
# YAML / project / stack resolution helpers
# -----------------------------------------------------------------------------

def load_yaml_file(path: str, required: bool = True) -> dict:
    """Load a YAML file and return a dict. Exit with a clear error if invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        if required:
            fail(f"Could not find required file: {path}")
        return {}
    except yaml.YAMLError as e:
        fail(f"Failed to parse YAML file {path}: {e}")

    if data is None:
        return {}
    if not isinstance(data, dict):
        fail(f"Expected YAML object at top level in {path}")
    return data

def get_project_name() -> str:
    """Read project name from Pulumi.yaml."""
    root = load_yaml_file("Pulumi.yaml")
    proj_name = root.get("name")
    if not proj_name:
        fail('Could not read project name from "Pulumi.yaml"')
    return proj_name

def get_current_stack() -> str:
    """
    Return the stack basename used for the local file (e.g. dev from ORG/dev).
    Uses PULUMI_STACK or pulumi stack output; falls back to a single local stack file.
    """
    stack_name = os.getenv("PULUMI_STACK")
    if stack_name:
        return stack_name.split("/", 1)[-1]

    run_result = None
    try:
        run_result = subprocess.run(
            ["pulumi", "stack"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail("Pulumi CLI not found. Install Pulumi or run this from the Pulumi container.")
    except subprocess.CalledProcessError:
        pass
    if run_result and run_result.stdout:
        regex_match = re.search(r"Current stack is ([^\s:]+):", run_result.stdout)
        if regex_match:
            return regex_match.group(1).split("/", 1)[-1]

    candidates = [
        f for f in os.listdir(".")
        if f.startswith("Pulumi.") and f.endswith(".yaml") and f != "Pulumi.yaml"
    ]
    if len(candidates) == 1:
        return candidates[0].replace("Pulumi.", "").replace(".yaml", "")

    fail("No stack is selected. Run 'pulumi stack select' or 'pulumi stack init' first.")

def get_stack_file_path(basename: str) -> str:
    """Return the path to the local stack config file (e.g. Pulumi.dev.yaml)."""
    return f"Pulumi.{basename}.yaml"

def apply_project_namespace(defaults_map: dict, project_name: str) -> dict:
    """Apply project namespace to keys that do not contain ':'."""
    out = {}
    for k, v in defaults_map.items():
        out[k if ":" in k else f"{project_name}:{k}"] = v
    return out

# -----------------------------------------------------------------------------
# default_vars merge helpers (seed default config into stack config)
# -----------------------------------------------------------------------------

def seed_value(default_value, existing_value, path_parts: list, report: dict):
    """
    Merge one default_vars branch with existing stack config.

    Returns (output_value, should_write). If the stack already has a value, we keep it and
    note "already_set". __REQUIRED__/__OPTIONAL__/__SECRET__ placeholders become report entries.
    """
    config_path = "/".join(path_parts)

    # Branch: stack already defines this path — merge nested dicts or keep as-is.
    if existing_value is not CONFIG_MISSING:
        if isinstance(default_value, dict) and isinstance(existing_value, dict):
            seeded = {}
            for key, value in default_value.items():
                child_existing = existing_value.get(key, CONFIG_MISSING)
                child_value, should_write = seed_value(
                    value,
                    child_existing,
                    path_parts + [key],
                    report,
                )
                if should_write:
                    seeded[key] = child_value

            for key, value in existing_value.items():
                if key not in seeded:
                    seeded[key] = copy.deepcopy(value)

            return seeded, True

        report["already_set"].append(config_path)
        return copy.deepcopy(existing_value), True

    # No existing value: seed from default tree, or record placeholder tokens for the UI.
    if isinstance(default_value, dict):
        seeded = {}
        for key, value in default_value.items():
            child_value, should_write = seed_value(
                value,
                CONFIG_MISSING,
                path_parts + [key],
                report,
            )
            if should_write:
                seeded[key] = child_value

        if seeded:
            return seeded, True
        return None, False

    if default_value == REQUIRED_TOKEN:
        report["must_set"].append(config_path)
        return None, False

    if default_value == OPTIONAL_TOKEN:
        report["optional_set"].append(config_path)
        return None, False

    if default_value == SECRET_TOKEN:
        report["secret_set"].append(config_path)
        return None, False

    return copy.deepcopy(default_value), True

def merge_defaults_into_config(defaults_map: dict, stack_config: object, project_name: str) -> tuple[dict, dict]:
    """
    Produce a new config mapping by applying default_vars on top of the stack's current config.

    Keys in defaults get project prefix when needed; keys only present in the stack file are preserved
    at the end. The report drives orange/cyan lists in the menu (must_set, optional_set, etc.).
    """
    # stack_config is `object` so runtime YAML (non-dict) is possible; type checkers would treat a `dict` param as always dict and mark the guard unreachable.
    if not isinstance(stack_config, dict):
        fail("Stack file 'config' section must be a YAML mapping.")

    namespaced = apply_project_namespace(defaults_map, project_name)
    report = {
        "must_set": [],
        "optional_set": [],
        "secret_set": [],
        "already_set": [],
    }
    merged = {}

    # First pass: every default key — seed or skip based on existing stack values.
    for key, value in namespaced.items():
        current = stack_config.get(key, CONFIG_MISSING)
        out_val, write = seed_value(value, current, [key], report)
        if write:
            merged[key] = out_val

    # Second pass: carry forward stack-only keys (e.g. secrets or keys not in default_vars).
    for key, value in stack_config.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)

    return merged, report

def emit_config_key_list(keys: list[str], project_name: str, color_code: str | None = None) -> None:
    """Print normalized keys for `pulumi config set` (optionally colorized)."""
    ns_prefix = f"{project_name}:"
    lines = []
    for k in keys:
        suffix = k[len(ns_prefix) :] if k.startswith(ns_prefix) else k
        lines.append(suffix.replace("/", "."))
    for item in sorted(lines):
        if color_code:
            msg(item, color_code)
        else:
            print(item)

# -----------------------------------------------------------------------------
# Environment detection
# -----------------------------------------------------------------------------

def detect_azure_environment(requirements_path: str = "requirements.txt") -> bool:
    """
    Detect whether this project is using an Azure Pulumi provider by scanning requirements.txt.
    Looks for pulumi-azure or pulumi-azure-native.
    """
    try:
        with open(requirements_path, "r", encoding="utf-8") as f:
            contents = f.read()
    except FileNotFoundError:
        msg_stderr(
            f"WARNING : requirements file not found at {requirements_path!r}; "
            "cannot auto-detect Azure environment.",
            COLOR_ORANGE,
        )
        return False

    has_pulumi_azure = "pulumi-azure" in contents
    has_pulumi_azure_native = "pulumi-azure-native" in contents

    if has_pulumi_azure or has_pulumi_azure_native:
        msg("INFO : Azure Pulumi provider detected from requirements.txt", COLOR_CYAN)
        return True

    msg(
        "WARNING : No Azure Pulumi provider (pulumi-azure or pulumi-azure-native) found in requirements.txt",
        COLOR_ORANGE,
    )
    return False


# -----------------------------------------------------------------------------
# Platform and Azure special variables
# -----------------------------------------------------------------------------
# "Special" variables are nested objects/lists in stack config (not a single pulumi config string).
# The menu injects templates via build_azure_* helpers so shapes match __main__.py.
# route_tables is handled separately: submenu appends routes instead of replacing the whole object.

PLATFORM_AZURE = "azure"

# Config keys (without project prefix) that are complex for Azure. Used to flag in UI.
SPECIAL_VARIABLES_AZURE = {
    "hub_nsg_rules",
    "route_tables",
    "peerings",
    "cloud_network_space",
    "vpn_gw_parameters",
    "local_gw_parameters",
    "palo_alto_vm",
}

def coerce_cidr(value: str) -> str:
    """Strip CIDR text for config fields (validation happens where needed, e.g. normalize_cidr)."""
    return str(value).strip() if value is not None else ""

def coerce_ip(value: str) -> str:
    """Normalize IP or CIDR for config. Returns stripped string."""
    return str(value).strip() if value is not None else ""

def coerce_int(value) -> int:
    """Coerce to int for config (e.g. bgp_asn, priority)."""
    if isinstance(value, int):
        return value
    return int(str(value).strip()) if value not in (None, "") else 0

def coerce_bool(value) -> bool:
    """Coerce to bool for config."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower() if value not in (None, "") else ""
    return s in ("true", "yes", "1", "on")

def get_special_variable_base_key(config_path: str, project_name: str) -> str | None:
    """
    If config_path refers to an Azure special variable, return its base key (e.g. hub_nsg_rules).
    config_path may be 'project:key' or 'project:key/subkey'. Returns None if not special.
    """
    prefix = f"{project_name}:"
    if not config_path.startswith(prefix):
        base = config_path.split("/")[0]
        return base if base in SPECIAL_VARIABLES_AZURE else None
    rest = config_path[len(prefix) :]
    base = rest.split("/")[0].split(".")[0]
    return base if base in SPECIAL_VARIABLES_AZURE else None

def is_special_variable(config_path: str, project_name: str, platform: str = PLATFORM_AZURE) -> bool:
    """Return True if this config path is a special (complex) variable for the given platform."""
    if platform != PLATFORM_AZURE:
        return False
    return get_special_variable_base_key(config_path, project_name) is not None

def build_azure_cloud_network_space(name: str = "", cidr: str = "") -> dict:
    """Build cloud_network_space dict for Azure. Expects name (str) and cidr (str, e.g. 10.10.0.0/20)."""
    return {
        "name": str(name).strip() if name else "",
        "cidr": coerce_cidr(cidr) or "",
    }

def build_azure_vpn_gw_parameters(
    bgp_asn: int = 0,
    bgp_peering_address1: str = "",
    bgp_peering_address2: str = "",
) -> dict:
    """Build vpn_gw_parameters dict for Azure. BGP ASN is int; peering addresses are strings (IPs)."""
    return {
        "bgp_asn": coerce_int(bgp_asn),
        "bgp_peering_address1": coerce_ip(bgp_peering_address1),
        "bgp_peering_address2": coerce_ip(bgp_peering_address2),
    }

def build_azure_local_gw_parameters(
    connection_ip: str = "",
    bgp_asn: int = 0,
    bgp_peering_address: str = "",
) -> dict:
    """Build local_gw_parameters dict for Azure. connection_ip and bgp_peering_address are IP strings; bgp_asn is int."""
    return {
        "connection_ip": coerce_ip(connection_ip),
        "bgp_asn": coerce_int(bgp_asn),
        "bgp_peering_address": coerce_ip(bgp_peering_address),
    }

def build_azure_palo_alto_vm(vm_name: str = "", pub_ip_name: str = "", admin_username: str = "azadmin") -> dict:
    """Build palo_alto_vm dict for Azure. All string fields."""
    return {
        "vm_name": str(vm_name).strip() if vm_name else "",
        "pub_ip_name": str(pub_ip_name).strip() if pub_ip_name else "",
        "admin_username": str(admin_username).strip() if admin_username else "azadmin",
    }

def build_azure_hub_nsg_rules() -> list:
    """
    Build default hub_nsg_rules list for Azure. Structure matches __main__.py build_hub_nsg_rules.
    Each rule: name, description, protocol, source_port_range, destination_port_range,
    source_address_prefix or source_address_prefix_ref, destination_address_prefix or destination_address_prefix_ref,
    access (Allow/Deny), priority (int), direction (Inbound/Outbound).
    """
    return [
        {
            "name": "Allow-Outside-From-IP",
            "description": "Rule",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix_ref": "on_prem_source_ip_range",
            "destination_address_prefix": "*",
            "access": "Allow",
            "priority": 100,
            "direction": "Inbound",
        },
        {
            "name": "Allow-Intra",
            "description": "Allow intra network traffic",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix_ref": "vnet",
            "destination_address_prefix": "*",
            "access": "Allow",
            "priority": 101,
            "direction": "Inbound",
        },
        {
            "name": "Default-Deny-If-No-Match",
            "description": "Rule",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix": "*",
            "destination_address_prefix": "*",
            "access": "Deny",
            "priority": 200,
            "direction": "Inbound",
        },
    ]

def build_azure_route_tables() -> dict:
    """
    Build default route_tables dict for Azure. Structure matches __main__.py:
    VnetToFw, FwToOutbound, FwToOnPrem_VNETs. Each value is a list of route dicts.
    Each route: name, address_prefix or address_prefix_ref, next_hop_type, next_hop_ip_ref (trust_nic/untrust_nic).
    """
    return {
        "VnetToFw": [
            {"name": "TEST-to-FW-Route1", "address_prefix": "0.0.0.0/0", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "TEST-to-FW-Route2", "address_prefix_ref": "on_prem_source_ip_range", "next_hop_type": "VirtualNetworkGateway"},
        ],
        "FwToOutbound": [
            {"name": "FW-to-Outbound-Route1", "address_prefix": "0.0.0.0/0", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "FW-to-Outbound-Route2", "address_prefix_ref": "on_prem_source_ip_range", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "FW-to-Outbound-Route3", "address_prefix_ref": "untrust_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        ],
        "FwToOnPrem_VNETs": [
            {"name": "FW-to-OnPrem_VNETs-Route1", "address_prefix_ref": "hub1_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "FW-to-OnPrem_VNETs-Route2", "address_prefix_ref": "hub2_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "Untrust-to-Trust-Route1", "address_prefix_ref": "trust_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "Azure-Drop", "address_prefix": "10.12.0.0/16", "next_hop_type": "None"},
        ],
    }

def build_azure_route_tables_for_stack(stack_file: str) -> dict:
    """
    Build default route tables using stack-specific values.

    - Route names with TEST-* are replaced by network_resource_prefix.
    - Azure-Drop CIDR is computed as /16 from the stack vnet value.
    """
    tables = copy.deepcopy(build_azure_route_tables())
    fallback_prefix = "TEST"

    try:
        project_name = get_project_name()
        data = load_yaml_file(stack_file, required=False)
        config = data.get("config") or {}

        prefix_key = f"{project_name}:network_resource_prefix"
        prefix_value = str(config.get(prefix_key) or config.get("network_resource_prefix") or "").strip()
        route_prefix = prefix_value if prefix_value else fallback_prefix
        for route in tables.get("VnetToFw", []):
            name = str(route.get("name") or "")
            if name.startswith("TEST-"):
                route["name"] = name.replace("TEST-", f"{route_prefix}-", 1)

        vnet_key = f"{project_name}:vnet"
        vnet_raw = str(config.get(vnet_key) or config.get("vnet") or "").strip()
        if vnet_raw:
            vnet_net = ipaddress.ip_network(vnet_raw, strict=False)
            azure_drop_cidr = str(vnet_net.supernet(new_prefix=16))
            for route in tables.get("FwToOnPrem_VNETs", []):
                if route.get("name") == "Azure-Drop":
                    route["address_prefix"] = azure_drop_cidr
                    break
    except Exception:
        pass

    return tables

def build_azure_peerings() -> list:
    """
    Build default peerings list for Azure. Each entry: name, remote_vnet_id, cidr (all strings).
    __main__.py expects list of dicts with those keys. Empty list by default.
    """
    return []

def normalize_cidr(cidr: str) -> str:
    """Normalize a CIDR string (e.g. '10.0.0.0/24') into canonical form."""
    try:
        return str(ipaddress.ip_network(str(cidr).strip(), strict=False))
    except Exception:
        fail(f"Invalid CIDR: {cidr!r}. Expected something like '10.0.0.0/24'.")


def normalize_route_destination_prefix(user_input: str) -> str:
    """
    Turn user input into a valid route address_prefix for stack config.

    Azure UDRs need a CIDR; '*' is accepted as shorthand for the default route 0.0.0.0/0.
    """
    raw = str(user_input).strip()
    if raw == "*":
        msg("Using 0.0.0.0/0 for route destination (same as typing the default route CIDR).", COLOR_CYAN)
        return "0.0.0.0/0"
    return normalize_cidr(raw)

def get_stack_config_value(config: dict, config_key: str):
    """
    Return a config value from stack YAML.

    Pulumi stacks namespace keys with the project name (e.g. '<project>:route_tables').
    This helper also supports falling back to an unprefixed key (e.g. 'route_tables').
    """
    if config_key in config:
        return config.get(config_key)
    if ":" in config_key:
        unprefixed = config_key.split(":", 1)[1]
        return config.get(unprefixed)
    return config.get(config_key)

def route_tables_add_route_submenu(stack_file: str, route_tables_config_key: str) -> None:
    """
    Interactive submenu to append a single route to one of the three Azure route tables.

    This does NOT overwrite the full route_tables structure; it appends to the existing
    list for the selected route table.

    After name, destination CIDR (* → 0.0.0.0/0), and next-hop fields, shows a preview and
    asks for confirmation before writing YAML (n = discard and re-enter for the same table).
    """
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}

    route_tables = (
        get_stack_config_value(config, route_tables_config_key)
        or build_azure_route_tables()
    )
    # Ensure expected keys exist even if the config is partially filled.
    for k in ("VnetToFw", "FwToOutbound", "FwToOnPrem_VNETs"):
        if k not in route_tables or not isinstance(route_tables.get(k), list):
            route_tables[k] = []

    # Default next hop settings chosen to match the patterns already used in Pulumi.dev.yaml:
    # - Trust-side routes typically point to trust_nic
    # - Untrust-side routes typically point to untrust_nic
    next_hop_defaults = {
        "VnetToFw": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        "FwToOutbound": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        "FwToOnPrem_VNETs": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
    }

    while True:
        msg("route_tables: add a new route to which table?", COLOR_CYAN)
        msg("  1) Add route to VnetToFw", COLOR_CYAN)
        msg("  2) Add route to FwToOutbound", COLOR_CYAN)
        msg("  3) Add route to FwToOnPrem_VNETs", COLOR_CYAN)
        msg("  4) Load default route tables template", COLOR_CYAN)
        msg("  0) Back", COLOR_CYAN)
        msg("")
        try:
            raw = input("Select an option [0-4]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if raw == "0" or quit_input_detected(raw):
            return
        if not raw.isdigit() or int(raw) not in (1, 2, 3, 4):
            msg("Invalid selection.", COLOR_ORANGE)
            continue

        choice = int(raw)
        if choice == 4:
            defaults = build_azure_route_tables_for_stack(stack_file)
            write_config_value_to_stack_file(stack_file, route_tables_config_key, defaults)
            msg("Loaded default route tables template into stack config.", COLOR_GREEN)
            continue

        table_key = {1: "VnetToFw", 2: "FwToOutbound", 3: "FwToOnPrem_VNETs"}[choice]

        # Build one route with next-hop + confirm so we do not write YAML until the user is done.
        while True:
            defaults = next_hop_defaults[table_key]
            try:
                route_name = input(f"Route name for {table_key} (blank = auto): ").strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return

            try:
                cidr_raw = input(
                    "Destination CIDR for the route (e.g. 10.0.0.0/24, or * for 0.0.0.0/0): "
                ).strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            if not cidr_raw:
                msg("CIDR cannot be empty (use * for default route 0.0.0.0/0).", COLOR_ORANGE)
                continue
            cidr = normalize_route_destination_prefix(cidr_raw)

            if not route_name:
                idx = len(route_tables.get(table_key) or [])
                route_name = f"{table_key}-Route{idx + 1}"

            try:
                nth_default = defaults["next_hop_type"]
                next_hop_type_raw = input(
                    f"Next hop type (Enter = {nth_default}; e.g. VirtualAppliance, VirtualNetworkGateway, None): "
                ).strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            next_hop_type = next_hop_type_raw or nth_default

            next_hop_ip_ref = None
            if next_hop_type == "VirtualAppliance":
                try:
                    ref_default = defaults["next_hop_ip_ref"]
                    next_hop_ip_ref = input(
                        f"Next hop IP ref (Enter = {ref_default}; trust_nic or untrust_nic): "
                    ).strip() or ref_default
                except EOFError:
                    msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                    return

            route_entry: dict = {
                "name": route_name,
                # Literal CIDR for this flow (not address_prefix_ref).
                "address_prefix": cidr,
                "next_hop_type": next_hop_type,
            }
            if next_hop_ip_ref:
                route_entry["next_hop_ip_ref"] = next_hop_ip_ref

            msg("--- Route preview (not saved yet) ---", COLOR_CYAN)
            msg(f"  Table: {table_key}", COLOR_CYAN)
            msg(f"  name: {route_entry['name']}", COLOR_CYAN)
            msg(f"  address_prefix: {route_entry['address_prefix']}", COLOR_CYAN)
            msg(f"  next_hop_type: {route_entry['next_hop_type']}", COLOR_CYAN)
            if next_hop_ip_ref:
                msg(f"  next_hop_ip_ref: {next_hop_ip_ref}", COLOR_CYAN)
            msg("", COLOR_CYAN)

            try:
                save_raw = input("Save this route to the stack file? [Y/n] (n = discard and re-enter): ").strip().lower()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            if save_raw == "n":
                msg("Discarded. Re-enter route details for the same table.", COLOR_ORANGE)
                continue

            route_tables[table_key].append(route_entry)
            write_config_value_to_stack_file(stack_file, route_tables_config_key, route_tables)
            msg(f"Added route '{route_name}' to {table_key}.", COLOR_GREEN)
            break

def derive_route_parts_from_peering_name(peering_name: str) -> tuple[str, str]:
    """
    Derive (local_prefix, remote_part) from a peering name.

    Example:
      peering_name = 'HUB-to-DEV-ORG-WEST-1'
      remote_part   = 'DEV-ORG-WEST-1'
      local_prefix  = 'DEV'   (first token of remote_part)
    """
    peering_name = str(peering_name).strip()
    if "to-" in peering_name:
        remote_part = peering_name.split("to-", 1)[1].strip()
        # If there are multiple 'to-' tokens, keep the last part.
        remote_part = remote_part.split("to-")[-1].strip()
    else:
        remote_part = peering_name
    local_prefix = remote_part.split("-", 1)[0] if "-" in remote_part else remote_part
    return local_prefix, remote_part

def add_peering_and_routes_to_stack(active_stack: dict) -> None:
    """
    Add a peering entry (peerings list) and append matching routes into:
      - VnetToFw
      - FwToOnPrem_VNETs

    This mirrors the example in Pulumi.dev.yaml where route_tables entries use:
      address_prefix_ref: peerings.<index>.cidr

    Note: for new peerings we only update the trust->FW and FW->on-prem route tables.
    FwToOutbound is intentionally left unchanged (not needed for new peerings).
    """
    # Steps: load YAML → validate inputs → append peering → add routes with peerings.<n>.cidr refs → write file.
    stack_file = active_stack["stack_file"]
    project_name = get_project_name()
    config_key_peerings = f"{project_name}:peerings"
    config_key_route_tables = f"{project_name}:route_tables"

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}

    peerings = get_stack_config_value(config, config_key_peerings) or []
    if not isinstance(peerings, list):
        peerings = []

    route_tables = get_stack_config_value(config, config_key_route_tables) or build_azure_route_tables()
    # Make sure the expected route table keys exist.
    for k in ("VnetToFw", "FwToOutbound", "FwToOnPrem_VNETs"):
        if k not in route_tables or not isinstance(route_tables.get(k), list):
            route_tables[k] = []

    try:
        peering_name = input("Peering name (e.g. HUB-to-DEV-ORG-WEST-1): ").strip()
        remote_vnet_id = input("remote_vnet_id (full Azure resource id): ").strip()
        cidr_raw = input("CIDR for the peered range (e.g. 10.100.4.0/24): ").strip()
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return
    if not peering_name or not remote_vnet_id or not cidr_raw:
        msg("Peering name, remote_vnet_id, and CIDR are required.", COLOR_ORANGE)
        return
    cidr = normalize_cidr(cidr_raw)

    # Prevent duplicates by matching on (name, remote_vnet_id, cidr).
    # Do not fail if an existing entry is malformed; just skip it for the duplicate check.
    for p in peerings:
        existing_raw = p.get("cidr", "")
        if p.get("name") == peering_name and p.get("remote_vnet_id") == remote_vnet_id and existing_raw:
            try:
                existing_cidr = str(ipaddress.ip_network(str(existing_raw), strict=False))
            except Exception:
                existing_cidr = None
            if existing_cidr == cidr:
                msg("A peering with the same name/remote_vnet_id/cidr already exists. Skipping.", COLOR_CYAN)
                return

    peering_index = len(peerings)
    peering_ref = f"peerings.{peering_index}.cidr"
    local_prefix, remote_part = derive_route_parts_from_peering_name(peering_name)

    # Append peering first so the new index is correct in the route refs.
    peerings.append({"name": peering_name, "remote_vnet_id": remote_vnet_id, "cidr": cidr})

    vnet_to_fw_ref_route = {
        "name": f"{local_prefix}-to-{remote_part}-Route1",
        "address_prefix_ref": peering_ref,
        "next_hop_type": "VirtualAppliance",
        "next_hop_ip_ref": "trust_nic",
    }
    fw_to_onprem_ref_route = {
        "name": f"FW-to-{remote_part}-Route1",
        "address_prefix_ref": peering_ref,
        "next_hop_type": "VirtualAppliance",
        "next_hop_ip_ref": "untrust_nic",
    }

    # Only add routes if they aren't already present for this ref.
    # We check by the computed address_prefix_ref (peerings.<index>.cidr) so the
    # route logic stays consistent with the existing Pulumi.dev.yaml style.
    existing_vnet_to_fw = [
        r for r in (route_tables.get("VnetToFw") or []) if r.get("address_prefix_ref") == peering_ref
    ]
    if not existing_vnet_to_fw:
        route_tables["VnetToFw"].append(vnet_to_fw_ref_route)
    else:
        msg("VnetToFw already has a route for this peering CIDR; not adding.", COLOR_CYAN)

    existing_fw_to_onprem = [
        r for r in (route_tables.get("FwToOnPrem_VNETs") or []) if r.get("address_prefix_ref") == peering_ref
    ]
    if not existing_fw_to_onprem:
        route_tables["FwToOnPrem_VNETs"].append(fw_to_onprem_ref_route)
    else:
        msg("FwToOnPrem_VNETs already has a route for this peering CIDR; not adding.", COLOR_CYAN)

    # Write back both configs.
    config[config_key_peerings] = peerings
    config[config_key_route_tables] = route_tables
    data["config"] = config
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    msg("Added peering and updated route tables (VnetToFw, FwToOnPrem_VNETs).", COLOR_GREEN)

def add_hub_nsg_rule_to_stack(active_stack: dict) -> None:
    """
    Append a new rule to hub_nsg_rules in the selected complete stack.

    The rule structure matches __main__.py expectations:
      - name, description
      - protocol
      - source_port_range, destination_port_range
      - source_address_prefix(_ref)
      - destination_address_prefix(_ref)
      - access, priority, direction

    Suggests the next free priority in 100–199; auto-names Allow-Outside-From-IP-N when name is blank;
    default source ref:on_prem_source_ip_range when source is blank.
    Protocol defaults to '*'. Source/destination port ranges default to '*' (Azure NSG wildcard).
    Destination address prefix defaults to '*' unless you enter a CIDR/IP or ref:vnet.
    """
    # hub_nsg_rules is a nested list/dict, so we update the stack YAML directly
    # (instead of using `pulumi config set --path`, which is awkward for complex objects).
    stack_file = active_stack["stack_file"]
    project_name = get_project_name()
    config_key_hub = f"{project_name}:hub_nsg_rules"

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    rules = get_stack_config_value(config, config_key_hub) or []
    if not isinstance(rules, list):
        rules = []
    if not rules:
        rules = build_azure_hub_nsg_rules()

    # --- Suggest next inbound Allow rule priority below the default deny (200) ---
    # Pick the smallest available priority in the "100-series" so the new rule
    # naturally stays ahead of the default deny rule (priority 200).
    existing_priorities: set[int] = set()
    for r in rules:
        pr = r.get("priority")
        try:
            existing_priorities.add(int(pr))
        except Exception:
            pass
    suggested_priority = None
    for p in range(100, 200):
        if p not in existing_priorities:
            suggested_priority = p
            break

    # --- Default name Allow-Outside-From-IP, Allow-Outside-From-IP-2, ... from existing rule names ---
    # Default rule name when user leaves NSG rule name blank.
    # We follow existing naming in Pulumi.dev.yaml, where:
    # - 'Allow-Outside-From-IP' is treated as index 1
    # - 'Allow-Outside-From-IP-2' is index 2
    # So the next generated name becomes 'Allow-Outside-From-IP-(max+1)'.
    allow_outside_prefix = "Allow-Outside-From-IP"
    max_allow_outside_index = 0
    for r in rules:
        rname = (r or {}).get("name") or ""
        if rname == allow_outside_prefix:
            max_allow_outside_index = max(max_allow_outside_index, 1)
            continue
        m = re.match(rf"^{re.escape(allow_outside_prefix)}-(\\d+)$", str(rname).strip())
        if m:
            try:
                max_allow_outside_index = max(max_allow_outside_index, int(m.group(1)))
            except Exception:
                pass
    next_allow_outside_index = max_allow_outside_index + 1 if max_allow_outside_index else 1
    default_rule_name = f"{allow_outside_prefix}-{next_allow_outside_index}"

    try:
        # Prompts: blanks get defaults after validation (name, source, priority, etc.).
        # Name can be left blank; we'll auto-generate it after we compute priority.
        name = input("NSG rule name (e.g. Allow-App-Servers): ").strip()
        description = input("Description (blank = 'Rule'): ").strip() or "Rule"
        protocol = input(
            "Protocol ('*' default, e.g. * Tcp Udp Icmp): "
        ).strip() or "*"
        source_port_range = input("Source port range ('*' default, e.g. * or 80 or 80-443): ").strip() or "*"
        destination_port_range = input(
            "Destination port range ('*' default, e.g. * or 443): "
        ).strip() or "*"
        priority_raw = input(
            f"Priority (int, must be < 200). Suggested: {suggested_priority if suggested_priority is not None else 150}. Press Enter to use suggested: "
        ).strip()
        direction = input("Direction ('Inbound'/'Outbound', default Inbound): ").strip() or "Inbound"
        access = input("Access ('Allow'/'Deny', default Allow): ").strip() or "Allow"
        source_raw = input("Source (use 'ref:on_prem_source_ip_range' or 'ref:vnet' or a CIDR/IP or '*'): ").strip()
        destination_address_raw = input(
            "Destination address prefix ('*' default, or CIDR/IP, or ref:vnet): "
        ).strip() or "*"
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return
    try:
        priority = int(priority_raw) if priority_raw else suggested_priority
    except Exception:
        msg("Priority must be an integer.", COLOR_ORANGE)
        return

    if priority is None or priority_raw == "" and suggested_priority is None:
        msg("No available priority in the 100-199 range; you must choose a priority < 200 manually.", COLOR_ORANGE)
        return
    if priority >= 200:
        msg("Priority must be < 200 so it stays before the default deny rule (priority 200).", COLOR_ORANGE)
        return

    # If the user left fields blank, apply defaults now.
    if not name:
        name = default_rule_name
    if not source_raw:
        # Default to allowing from the configured on-prem source IP range reference.
        source_raw = "ref:on_prem_source_ip_range"

    if not name or not source_raw:
        msg("Rule name and source are required (even after applying defaults).", COLOR_ORANGE)
        return

    # --- Build dict: literal vs ref:* for source/destination matches __main__.py conventions ---
    route_rule = {
        "name": name,
        "description": description,
        "protocol": protocol,
        "source_port_range": source_port_range,
        "destination_port_range": destination_port_range,
        "access": access,
        "priority": priority,
        "direction": direction,
    }

    # Source address can be either a literal prefix or a reference key.
    if source_raw.startswith("ref:"):
        route_rule["source_address_prefix_ref"] = source_raw.split("ref:", 1)[1].strip()
    else:
        route_rule["source_address_prefix"] = source_raw

    # Destination address prefix (literal *, CIDR/IP, or ref:config_key).
    if destination_address_raw.startswith("ref:"):
        route_rule["destination_address_prefix_ref"] = destination_address_raw.split("ref:", 1)[1].strip()
    else:
        route_rule["destination_address_prefix"] = destination_address_raw

    rules.append(route_rule)
    write_config_value_to_stack_file(stack_file, config_key_hub, rules)
    msg(f"Added hub NSG rule '{name}'.", COLOR_GREEN)

def hub_nsg_rules_submenu(stack_full_name: str, stack_file: str, hub_nsg_rules_config_key: str) -> None:
    """
    Interactive submenu for hub_nsg_rules:
      - add an individual NSG rule, or
      - load the default NSG rules template.
    """
    while True:
        msg("hub_nsg_rules: choose an action", COLOR_CYAN)
        msg("  1) Add individual hub NSG rule", COLOR_CYAN)
        msg("  2) Load default hub NSG rules template", COLOR_CYAN)
        msg("  0) Back", COLOR_CYAN)
        msg("")
        try:
            raw = input("Select an option [0-2]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if raw == "0" or quit_input_detected(raw):
            return
        if not raw.isdigit() or int(raw) not in (1, 2):
            msg("Invalid selection.", COLOR_ORANGE)
            continue

        choice = int(raw)
        if choice == 1:
            add_hub_nsg_rule_to_stack(
                {"full_name": stack_full_name, "stack_file": stack_file}
            )
            return

        defaults = build_azure_hub_nsg_rules()
        write_config_value_to_stack_file(stack_file, hub_nsg_rules_config_key, defaults)
        msg("Loaded default hub NSG rules template into stack config.", COLOR_GREEN)
        return

def get_azure_built_value_for_special_key(base_key: str) -> dict | list | None:
    """Map a special variable base name to its default template (dict/list) from the build_azure_* factories."""
    builders = {
        "cloud_network_space": build_azure_cloud_network_space,
        "vpn_gw_parameters": build_azure_vpn_gw_parameters,
        "local_gw_parameters": build_azure_local_gw_parameters,
        "palo_alto_vm": build_azure_palo_alto_vm,
        "hub_nsg_rules": build_azure_hub_nsg_rules,
        "route_tables": build_azure_route_tables,
        "peerings": build_azure_peerings,
    }
    fn = builders.get(base_key)
    return fn() if fn else None

def is_top_level_special_config_path(config_path: str, project_name: str) -> bool:
    """True if config_path is a top-level special key (no subpath), e.g. project:hub_nsg_rules."""
    if "/" in config_path:
        return False
    return get_special_variable_base_key(config_path, project_name) is not None

def write_config_value_to_stack_file(stack_file: str, config_key: str, value: dict | list) -> None:
    """Update one namespaced key under config: in Pulumi.<stack>.yaml (dict/list values for complex types)."""
    if not os.path.isfile(stack_file):
        fail(f"Stack file not found: {stack_file}")
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config")
    if config is None:
        data["config"] = config = {}
    config[config_key] = value
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# -----------------------------------------------------------------------------
# Stack discovery and inspection
# -----------------------------------------------------------------------------
# discover_stacks + inspect_stack power the checklist; get_config_report drives "set missing vars".

def discover_stacks():
    """
    Discover Pulumi stacks for this project.

    Merges stacks from two sources:
      1) `pulumi stack ls --json` (backend/remote view)
      2) local Pulumi.<stack>.yaml files (local file view)
    so stacks that exist only locally are still shown in the menu.

    Returns a list of dicts with:
      - full_name: the Pulumi stack identifier (may include org/project)
      - basename: the final path segment used in Pulumi.<basename>.yaml
      - stack_file: the local stack file path
    """
    # Keep the committed example config (Pulumi.sample.yaml) out of the interactive UI.
    # Treat it as documentation, not a real deployable stack.
    SAMPLE_STACK_BASENAME = "sample"

    stacks: list[dict] = []
    seen_basenames: set[str] = set()

    # Try Pulumi CLI first (remote/backend view).
    try:
        result = subprocess.run(
            ["pulumi", "stack", "ls", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout or "[]")
        for entry in data:
            # entry["name"] may be "stack", "org/stack", or "org/project/stack"
            full_name = entry.get("name") or ""
            if not full_name:
                continue
            basename = full_name.split("/")[-1]
            if basename == SAMPLE_STACK_BASENAME:
                continue
            if basename in seen_basenames:
                continue
            stack_file = f"Pulumi.{basename}.yaml"
            stacks.append(
                {
                    "full_name": full_name,
                    "basename": basename,
                    "stack_file": stack_file,
                }
            )
            seen_basenames.add(basename)
    except Exception:
        pass

    # Always add local stack files as well (local view), so stacks removed from
    # backend but still present on disk remain visible/manageable.
    for fname in sorted(os.listdir(".")):
        if fname.startswith("Pulumi.") and fname.endswith(".yaml") and fname != "Pulumi.yaml":
            basename = fname.replace("Pulumi.", "").replace(".yaml", "")
            if basename == SAMPLE_STACK_BASENAME:
                continue
            if basename in seen_basenames:
                continue
            stacks.append(
                {
                    "full_name": basename,
                    "basename": basename,
                    "stack_file": fname,
                }
            )
            seen_basenames.add(basename)

    return stacks

def inspect_stack(stack_info):
    """
    Summarize one stack for the checklist using the local Pulumi.<basename>.yaml (if present).

    Returns dict with: status (complete | incomplete | remote_only), has_kv_name, reasons,
    optional_missing. "complete" means required keys from default_vars are satisfied; Key Vault
    existence is handled separately via create_keyvault --check-only in interactive_menu.
    """
    stack_file = stack_info["stack_file"]
    reasons = []

    # If the local stack file does not exist, this may simply mean the stack is
    # managed or initialized on a different machine. Do not treat this as a
    # configuration error; just report it separately.
    if not os.path.isfile(stack_file):
        return {
            "status": "remote_only",
            "has_kv_name": False,
            "reasons": [f"Local stack file '{stack_file}' is not present on this machine."],
            "optional_missing": [],
        }

    # Load project name and stack config via existing helpers.
    project = get_project_name()
    stack_data = load_yaml_file(stack_file, required=False)
    config = stack_data.get("config") or {}

    # Check for key_vault_name config (required before we can create a Key Vault).
    kv_key_project = f"{project}:key_vault_name"
    kv_name = config.get(kv_key_project) or config.get("key_vault_name")
    has_kv_name = bool(kv_name)
    if not has_kv_name:
        reasons.append("Missing 'key_vault_name' in stack config (required before creating Azure Key Vault).")

    # Check required and optional variables from default_vars.yaml.
    optional_missing: list[str] = []
    try:
        defaults_path = "default_vars.yaml"
        if os.path.isfile(defaults_path):
            defaults_map = load_yaml_file(defaults_path, required=False)
            if defaults_map:
                _, report = merge_defaults_into_config(defaults_map, config, project)
                for config_path in report.get("must_set") or []:
                    key_display = config_path.replace("/", ".")
                    reasons.append(f"Missing required config: {key_display}")
                for config_path in report.get("optional_set") or []:
                    key_display = config_path.replace("/", ".")
                    optional_missing.append(f"Optional (not set): {key_display}")
    except Exception:
        # If merge fails (e.g. invalid config), do not fail the whole checklist.
        pass

    if reasons:
        return {"status": "incomplete", "has_kv_name": has_kv_name, "reasons": reasons, "optional_missing": optional_missing}
    return {"status": "complete", "has_kv_name": has_kv_name, "reasons": [], "optional_missing": optional_missing}

def get_config_report(stack_file: str) -> tuple[list[str], list[str]]:
    """
    Return (must_set, optional_set) for the stack from default_vars merge.
    Both are lists of config paths (with '/' for nesting). Empty lists if file missing or no defaults.
    """
    if not os.path.isfile(stack_file):
        return ([], [])
    try:
        project = get_project_name()
        stack_data = load_yaml_file(stack_file, required=False)
        config = stack_data.get("config") or {}
        defaults_path = "default_vars.yaml"
        if not os.path.isfile(defaults_path):
            return ([], [])
        defaults_map = load_yaml_file(defaults_path, required=False)
        if not defaults_map:
            return ([], [])
        _, report = merge_defaults_into_config(defaults_map, config, project)
        return (
            list(report.get("must_set") or []),
            list(report.get("optional_set") or []),
        )
    except Exception:
        return ([], [])

def get_missing_required_config(stack_file: str) -> list[str]:
    """
    Return list of config paths (with '/' for nesting) that are required in default_vars
    but not set in the stack. Empty if stack file missing or no defaults.
    """
    must_set, _ = get_config_report(stack_file)
    return must_set

def print_stack_checklist(
    stacks: list[dict] | None = None,
    summaries: dict[str, dict] | None = None,
    kv_exists: dict[str, bool] | None = None,
    azure_env: bool = False,
) -> None:
    """
    Print a checklist of all discovered stacks and their configuration status.

    When `azure_env` is true and `kv_exists` is provided, a stack will only be shown as
    "[OK]" when `key_vault_name` is configured AND the Key Vault is deploy-ready
    (exists and required secrets are present).
    """
    if stacks is None or summaries is None:
        stacks = discover_stacks()
        summaries = {s["full_name"]: inspect_stack(s) for s in stacks}

    if not stacks:
        msg("No Pulumi stacks found for this project.", COLOR_ORANGE)
        return

    if kv_exists is None:
        kv_exists = {}

    msg("Stack checklist:", COLOR_CYAN)
    for s in stacks:
        summary = summaries[s["full_name"]]
        label = s["full_name"]
        status = summary["status"]

        has_kv_name = bool(summary.get("has_kv_name", False))
        kv_found = kv_exists.get(label, False)
        local_stack = os.path.isfile(s["stack_file"])

        # If the stack is otherwise complete but the Key Vault is not deploy-ready,
        # don't show it as green/OK.
        if (
            status == "complete"
            and azure_env
            and has_kv_name
            and local_stack
            and not kv_found
        ):
            msg(f"  [INCOMPLETE] {label}", COLOR_ORANGE)
            msg("    - Azure Key Vault: NOT READY (missing vault and/or required secrets)", COLOR_ORANGE)
            continue

        if status == "complete":
            msg(f"  [OK] {label}", COLOR_GREEN)
        elif status == "remote_only":
            msg(f"  [REMOTE] {label}", COLOR_CYAN)
            for reason in summary["reasons"]:
                msg(f"    - {reason}", COLOR_CYAN)
        else:
            msg(f"  [INCOMPLETE] {label}", COLOR_ORANGE)
            for reason in summary["reasons"]:
                msg(f"    - {reason}", COLOR_ORANGE)
            for line in summary.get("optional_missing") or []:
                msg(f"    - {line}", COLOR_CYAN)
    msg("", COLOR_CYAN)


# -----------------------------------------------------------------------------
# Step wrappers (called from menu)
# -----------------------------------------------------------------------------
# Thin wrappers around merge/write and create_keyvault.main() with env argv manipulation.

def seed_default_vars(stack: str | None) -> None:
    """
    Seed Pulumi stack config from default_vars.yaml: merge defaults into the active stack
    without overwriting existing keys. Placeholders __REQUIRED__, __OPTIONAL__, __SECRET__
    are reported for setting via pulumi config. If stack is set, PULUMI_STACK is used.
    """
    original_stack_env = os.environ.get("PULUMI_STACK")
    try:
        if stack:
            os.environ["PULUMI_STACK"] = stack
        msg("STEP 1 : Seeding Pulumi stack config from default_vars.yaml", COLOR_CYAN)
        project_name = get_project_name()
        stack_basename = get_current_stack()
        stack_path = get_stack_file_path(stack_basename)

        defaults_map = load_yaml_file("default_vars.yaml")
        stack_file_existed = os.path.isfile(stack_path)
        stack_data = load_yaml_file(stack_path, required=False)
        stack_config = stack_data.get("config") or {}

        merged_config, report = merge_defaults_into_config(defaults_map, stack_config, project_name)
        stack_data["config"] = merged_config

        try:
            with open(stack_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
        except OSError as e:
            fail(f"Failed to write {stack_path}: {e}")

        msg("STEP 1 : default_vars.yaml applied successfully.", COLOR_GREEN)
        msg("WARNING : For nested keys use: pulumi config set --path a.b.c <value>", COLOR_ORANGE)

        if report["must_set"]:
            msg("WARNING : These keys have no default; set with 'pulumi config set <key> <value>'", COLOR_ORANGE)
            emit_config_key_list(report["must_set"], project_name, COLOR_ORANGE)

        if report["optional_set"]:
            msg("INFO : Optional keys; set when needed with 'pulumi config set'", COLOR_CYAN)
            emit_config_key_list(report["optional_set"], project_name, COLOR_CYAN)

        if report["secret_set"]:
            msg("WARNING : Set these as secrets: 'pulumi config set --secret <key> <value>'", COLOR_ORANGE)
            emit_config_key_list(report["secret_set"], project_name, COLOR_ORANGE)

        if report["already_set"]:
            msg("SUCCESS : Existing config keys were left unchanged", COLOR_GREEN)
            emit_config_key_list(report["already_set"], project_name, COLOR_GREEN)

        if stack_file_existed:
            msg(
                "INFO : Run 'pulumi preview' or 'pulumi up' to review or deploy changes.",
                COLOR_CYAN,
            )
    finally:
        if original_stack_env is not None:
            os.environ["PULUMI_STACK"] = original_stack_env
        elif "PULUMI_STACK" in os.environ:
            del os.environ["PULUMI_STACK"]

def create_az_kv(stack: str | None, yes_kv_provider: bool = False) -> None:
    """
    Create Azure Key Vault, IAM, and required secrets for the stack.

    This preserves the exact behavior of create_keyvault.py by delegating into
    its main() with a constructed argv, so no logic is duplicated here.
    """
    argv: list[str] = ["create_keyvault.py"]
    if stack:
        argv.extend(["--stack", stack])
    if yes_kv_provider:
        argv.append("--yes")

    msg("STEP 2 : Creating Azure Key Vault, IAM role, and required secrets", COLOR_CYAN)

    old_argv = sys.argv
    try:
        sys.argv = argv
        create_keyvault.main()
        msg("STEP 2 : Azure Key Vault and secrets are ready.", COLOR_GREEN)
    finally:
        sys.argv = old_argv


# -----------------------------------------------------------------------------
# Next on-prem network helper
# -----------------------------------------------------------------------------

CIDR_CHOICES = ["/24", "/25", "/26", "/27", "/28", "/29"]

def stack_has_cloud_network_space_key(stack_file: str) -> bool:
    """Return True if the stack config has cloud_network_space set (any value). Used to show on-prem menu option."""
    if not os.path.isfile(stack_file):
        return False
    try:
        project = get_project_name()
        data = load_yaml_file(stack_file, required=False)
        config = data.get("config") or {}
        key = f"{project}:cloud_network_space"
        return (key in config) or ("cloud_network_space" in config)
    except Exception:
        return False

def get_cloud_network_space(stack_file: str) -> dict | None:
    """Return the stack's cloud_network_space config (name, cidr), or None if missing/invalid."""
    project = get_project_name()
    data = load_yaml_file(stack_file, required=False)
    config = data.get("config") or {}
    key = f"{project}:cloud_network_space"
    value = config.get(key) or config.get("cloud_network_space")
    if isinstance(value, dict) and value.get("name") and value.get("cidr"):
        return {"name": str(value["name"]).strip(), "cidr": str(value["cidr"]).strip()}
    return None

def run_next_onprem_net(stack_full_name: str, cidr: str) -> None:
    """Run get_next_onprem_net.py with the given stack and CIDR mask (stack supplies cloud_network_space)."""
    script = "get_next_onprem_net.py"
    if not os.path.isfile(script):
        fail(f"Required script not found: {script}")
    msg(f"INFO : Checking next available on-prem network for stack {stack_full_name}, mask {cidr}", COLOR_CYAN)
    env = os.environ.copy()
    env["PULUMI_STACK"] = stack_full_name
    result = subprocess.run(
        [sys.executable, script, cidr, "--stack", stack_full_name],
        env=env,
        cwd=os.getcwd(),
        capture_output=False,
    )
    if result.returncode != 0:
        msg_stderr(f"Script {script} exited with code {result.returncode}.", COLOR_ORANGE)

def run_check_next_onprem_network() -> None:
    """Prompt for stack and CIDR; use stack's cloud_network_space, then run get_next_onprem_net.py."""
    stacks = discover_stacks()
    # Only stacks that have a local config file and have cloud_network_space (name, cidr) set.
    local_stacks = [
        s for s in stacks
        if os.path.isfile(s["stack_file"]) and get_cloud_network_space(s["stack_file"])
    ]
    if not local_stacks:
        msg("No stack has cloud_network_space (name, cidr) set. Set it in a stack config to use this option.", COLOR_ORANGE)
        return

    if len(local_stacks) == 1:
        chosen_stack = local_stacks[0]
        msg(f"Using stack: {chosen_stack['full_name']}", COLOR_CYAN)
    else:
        msg("Select a stack:", COLOR_CYAN)
        for i, s in enumerate(local_stacks, start=1):
            msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
        msg("")
        try:
            raw = input(f"Stack number [1-{len(local_stacks)}]: ").strip()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if not raw.isdigit() or int(raw) < 1 or int(raw) > len(local_stacks):
            msg("Invalid selection.", COLOR_ORANGE)
            return
        chosen_stack = local_stacks[int(raw) - 1]
    full_name = chosen_stack["full_name"]

    msg(f"Enter CIDR mask for the range (e.g. /24, /28). Choices: {', '.join(CIDR_CHOICES)}", COLOR_CYAN)
    while True:
        try:
            cidr_raw = input("CIDR [/28]: ").strip().lower() or "/28"
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if quit_input_detected(cidr_raw):
            raise SystemExit(0)
        if cidr_raw not in CIDR_CHOICES:
            # Accept bare numbers: 24 -> /24, 28 -> /28, etc.
            if cidr_raw.isdigit() and f"/{cidr_raw}" in CIDR_CHOICES:
                cidr_raw = f"/{cidr_raw}"
            else:
                msg(f"Invalid CIDR. Use one of: {', '.join(CIDR_CHOICES)}", COLOR_ORANGE)
                continue
        break
    run_next_onprem_net(full_name, cidr_raw)

    msg("", COLOR_CYAN)
    try:
        choice = input("Press Enter to return to menu, or q to quit: ").strip().lower()
    except EOFError:
        return
    if quit_input_detected(choice):
        raise SystemExit(0)


# -----------------------------------------------------------------------------
# Set required variables (one at a time)
# -----------------------------------------------------------------------------

def run_set_required_variables(stack_full_name: str, stack_file: str) -> None:
    """
    Loop: list missing required keys (and optional keys), let user pick one to set.

    Top-level Azure special keys: write YAML templates or open route_tables submenu.
    Simple keys: run `pulumi config set` or `pulumi config set --path` with PULUMI_STACK set.
    """
    env = os.environ.copy()
    env["PULUMI_STACK"] = stack_full_name
    project_name = get_project_name()

    while True:
        # Refresh from disk each iteration so prior sets take effect.
        missing, optional = get_config_report(stack_file)
        if not missing:
            msg("All required variables are already set for this stack.", COLOR_GREEN)
            return

        # Single numbered list: required rows first, then optional (for convenience).
        optional_sorted = sorted(set(optional))
        combined = [(p, True) for p in missing] + [(p, False) for p in optional_sorted]

        msg(f"Config for stack '{stack_full_name}':", COLOR_CYAN)
        n_required = len(missing)
        for i, (path, is_required) in enumerate(combined, start=1):
            if not is_required and i == n_required + 1:
                msg("  Optional (set when needed):", COLOR_CYAN)
            key_display = path.replace("/", ".")
            special_suffix = " (Azure special)" if is_special_variable(path, project_name) else ""
            color = COLOR_ORANGE if is_required else COLOR_CYAN
            msg(f"  {i}) {key_display}{special_suffix}", color)
        msg("  0) back to menu", COLOR_CYAN)
        msg("  q) quit", COLOR_CYAN)
        msg("")
        max_num = len(combined)
        try:
            raw = input(f"Number to set [0-{max_num}]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; returning to menu.", COLOR_ORANGE)
            return
        if raw == "0":
            return
        if quit_input_detected(raw):
            raise SystemExit(0)
        if not raw.isdigit():
            msg(f"Enter a number 0-{max_num} or q to quit.", COLOR_ORANGE)
            continue
        idx = int(raw)
        if idx < 1 or idx > max_num:
            msg(f"Invalid number. Use 0-{max_num} or q to quit.", COLOR_ORANGE)
            continue

        config_path, _ = combined[idx - 1]
        key_for_cmd = config_path.replace("/", ".")

        # Top-level Azure special variable: inject built structure into stack YAML.
        if is_top_level_special_config_path(config_path, project_name):
            base_key = get_special_variable_base_key(config_path, project_name)
            if base_key == "route_tables":
                try:
                    # route_tables is a complex object, so we provide a dedicated submenu
                    # that appends to one of the route table lists (instead of asking for
                    # the full YAML structure in one input).
                    route_tables_add_route_submenu(stack_file, config_path)
                except Exception as e:
                    msg_stderr(f"Failed to update route_tables: {e}", COLOR_RED)
                continue
            if base_key == "hub_nsg_rules":
                try:
                    # hub_nsg_rules can be set one rule at a time or loaded from defaults.
                    hub_nsg_rules_submenu(stack_full_name, stack_file, config_path)
                except Exception as e:
                    msg_stderr(f"Failed to update hub_nsg_rules: {e}", COLOR_RED)
                continue

            built = get_azure_built_value_for_special_key(base_key)
            if built is not None:
                try:
                    write_config_value_to_stack_file(stack_file, config_path, built)
                    msg(f"Injected Azure template for {key_for_cmd}. Edit Pulumi stack YAML to customize.", COLOR_GREEN)
                except Exception as e:
                    msg_stderr(f"Failed to write stack file: {e}", COLOR_RED)
                continue

        # Single-value or nested leaf: prompt and use pulumi config set.
        try:
            value = input(f"Value for {key_for_cmd}: ").strip()
        except EOFError:
            msg_stderr("Input closed; returning to menu.", COLOR_ORANGE)
            return
        if not value:
            msg("Value cannot be empty; skipping.", COLOR_ORANGE)
            continue

        use_path = "/" in config_path
        if use_path:
            result = subprocess.run(
                ["pulumi", "config", "set", "--path", key_for_cmd, value],
                env=env,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                ["pulumi", "config", "set", key_for_cmd, value],
                env=env,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            msg_stderr(result.stderr or "pulumi config set failed.", COLOR_RED)
            continue
        msg(f"Set {key_for_cmd}.", COLOR_GREEN)

        # Re-check; if no more missing, we're done.
        missing = get_missing_required_config(stack_file)
        if not missing:
            msg("All required variables are now set for this stack.", COLOR_GREEN)
            return


# -----------------------------------------------------------------------------
# Stack creation helper
# -----------------------------------------------------------------------------

def create_new_stack() -> None:
    """
    Prompt for a stack name, check for config file conflict, then run 'pulumi stack init'
    only if no conflict. Creates the local Pulumi.<stack>.yaml file for the new stack.
    """
    msg("Enter stack name (e.g. dev or ORG/mystack). Leave blank to use 'dev':", COLOR_CYAN)
    try:
        raw = input("Stack name: ").strip()
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return

    full_name = raw if raw else "dev"
    basename = full_name.split("/")[-1]
    stack_file = f"Pulumi.{basename}.yaml"

    # Check for file conflict before creating the stack. Do not run init if the
    # config file already exists for another stack.
    if os.path.isfile(stack_file):
        stacks = discover_stacks()
        others = [s["full_name"] for s in stacks if s["stack_file"] == stack_file]
        if others:
            msg_stderr(
                f"Config file '{stack_file}' already exists and is used by: {', '.join(others)}.",
                COLOR_RED,
            )
            msg_stderr(
                "Choose a different stack name (e.g. org/stackname) to avoid duplicate config files.",
                COLOR_ORANGE,
            )
            fail("Duplicate stack config file. Use a different stack name.")
        # File exists but no other stack in the backend uses it; we can still init and use it.
        # Fall through to init.

    try:
        subprocess.run(["pulumi", "stack", "init", full_name], check=False)
    except FileNotFoundError:
        fail('Pulumi CLI not found. Install Pulumi or run this from the Pulumi container.')

    current = get_current_stack_full()
    if not current:
        return

    # Ensure local config file exists for the new stack.
    cur_basename = current.split("/")[-1]
    cur_stack_file = f"Pulumi.{cur_basename}.yaml"

    if not os.path.isfile(cur_stack_file):
        try:
            with open(cur_stack_file, "w", encoding="utf-8") as f:
                f.write("config: {}\n")
        except OSError as e:
            fail(f"Failed to create local stack file '{cur_stack_file}': {e}")
        msg(f"INFO : Created local stack file '{cur_stack_file}' for stack '{current}'.", COLOR_CYAN)
    else:
        msg(f"INFO : Stack '{current}' is using local file '{cur_stack_file}'.", COLOR_CYAN)


# -----------------------------------------------------------------------------
# Helpers for current stack
# -----------------------------------------------------------------------------

def get_current_stack_full() -> str | None:
    """
    Get the current Pulumi stack identifier as understood by the CLI.

    Returns values like:
      - "dev"
      - "ORG/dev"
      - "ORG/azure-pa-hub-network/dev"
    or None if no current stack is selected.
    """
    env_stack = os.getenv("PULUMI_STACK")
    if env_stack:
        return env_stack.strip()

    try:
        result = subprocess.run(
            ["pulumi", "stack"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    text = result.stdout or ""
    match = re.search(r"Current stack is ([^\s:]+):", text)
    if match:
        return match.group(1).strip()
    return None


# -----------------------------------------------------------------------------
# Interactive menu
# -----------------------------------------------------------------------------

def interactive_menu() -> None:
    """
    Main UI loop until user quits.

    Each iteration: discover stacks, optionally run Key Vault existence checks, print checklist,
    then show one of two menus — (A) no incomplete local stacks: create stack / KV / advanced ops /
    on-prem helper; (B) incomplete stacks: pick active stack, show status line, actions for that stack.
    """
    azure_env = detect_azure_environment()
    kv_done_stacks: set[str] = set()
    kv_exists: dict[str, bool] = {}

    while True:
        # --- Refresh stack list and per-stack completeness (from default_vars merge) ---
        stacks = discover_stacks()
        summaries = {s["full_name"]: inspect_stack(s) for s in stacks}

        # --- Key Vault preflight (once per stack per session): used for checklist coloring and menu options ---
        # Preflight: for every local stack file with a configured key_vault_name,
        # check whether the Key Vault is deploy-ready (exists + required secrets).
        if azure_env:
            for s in stacks:
                full_name = s["full_name"]
                if not os.path.isfile(s["stack_file"]):
                    continue
                if not summaries.get(full_name, {}).get("has_kv_name", False):
                    continue
                if full_name in kv_exists:
                    continue
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "create_keyvault.py",
                            "--check-only",
                            "--stack",
                            full_name,
                        ],
                        cwd=os.getcwd(),
                        capture_output=True,
                        text=True,
                    )
                    kv_exists[full_name] = result.returncode == 0
                except Exception:
                    # If the check fails for any reason (e.g. az auth issues),
                    # treat as "not known" and default to offering creation.
                    kv_exists[full_name] = False

        print_stack_checklist(stacks=stacks, summaries=summaries, kv_exists=kv_exists, azure_env=azure_env)

        incomplete_stacks = [
            s for s in stacks if summaries[s["full_name"]]["status"] == "incomplete"
        ]

        current_stack_full = get_current_stack_full()
        # If there are any incomplete stacks, do not automatically prefer Pulumi's
        # currently-selected stack when entering Menu B. Pulumi's "current stack"
        # might be a different (complete) stack, which would hide the actions
        # needed to fill missing config for the incomplete ones.
        if incomplete_stacks:
            current_stack_full = None

        # ========== Menu A: everyone complete (or no stacks) — global actions + stack picker for some ops ==========
        # Case 1: no stacks or no incomplete stacks -> create / on-prem (only if a stack has cloud_network_space key) / quit.
        if not stacks or not incomplete_stacks:
            local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
            has_onprem_option = any(stack_has_cloud_network_space_key(s["stack_file"]) for s in local_stacks)

            # Find a complete stack (local file present) so we can offer peering/route updates.
            complete_local_stacks = [
                s
                for s in stacks
                if summaries.get(s["full_name"], {}).get("status") == "complete"
                and os.path.isfile(s["stack_file"])
            ]

            actions: list[tuple[str, callable]] = []
            actions.append(("Create new stack", create_new_stack))
            if has_onprem_option:
                actions.append(("Check for next available on-prem connecting network space", run_check_next_onprem_network))

            # Eligible stacks for advanced actions:
            # - config variables are complete (inspect_stack status == "complete")
            # - Key Vault is deploy-ready (kv_exists preflight)
            eligible_adv_stacks = [
                s
                for s in complete_local_stacks
                if summaries.get(s["full_name"], {}).get("has_kv_name", False)
                and kv_exists.get(s["full_name"], False)
            ]

            # Eligible stacks for creating a Key Vault:
            # - stack vars complete
            # - has key_vault_name configured
            # - preflight says the vault is not deploy-ready yet
            eligible_kv_create_stacks = [
                s
                for s in complete_local_stacks
                if summaries.get(s["full_name"], {}).get("has_kv_name", False)
                and not kv_exists.get(s["full_name"], False)
                and s["full_name"] not in kv_done_stacks
            ]

            def pick_stack(candidates: list[dict], prompt: str) -> dict:
                """Pick a stack; if only one candidate exists, auto-select it."""
                if not candidates:
                    fail("No eligible stacks found for this action.")
                if len(candidates) == 1:
                    return candidates[0]
                msg(prompt, COLOR_CYAN)
                for i, s in enumerate(candidates, start=1):
                    msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
                msg("  q) quit", COLOR_CYAN)
                msg("")
                try:
                    raw = input(f"Select stack [1-{len(candidates)}]: ").strip().lower()
                except EOFError:
                    msg_stderr("Input closed; exiting.", COLOR_ORANGE)
                    raise SystemExit(0)
                if quit_input_detected(raw):
                    raise SystemExit(0)
                if not raw.isdigit() or not (1 <= int(raw) <= len(candidates)):
                    msg("Invalid selection.", COLOR_ORANGE)
                    return pick_stack(candidates, prompt)
                return candidates[int(raw) - 1]

            if eligible_adv_stacks:
                actions.append(
                    (
                        "Add peering (and routes)",
                        lambda: add_peering_and_routes_to_stack(
                            pick_stack(eligible_adv_stacks, "Select stack to add peering/routes:")
                        ),
                    )
                )
                actions.append(
                    (
                        "Add hub NSG rule",
                        lambda: add_hub_nsg_rule_to_stack(
                            pick_stack(eligible_adv_stacks, "Select stack to add hub NSG rule:")
                        ),
                    )
                )

            if eligible_kv_create_stacks and azure_env:
                # Insert right after "Create new stack" for visibility.
                def create_kv_for_selected_stack():
                    chosen = pick_stack(
                        eligible_kv_create_stacks,
                        "Select stack to create Azure Key Vault:",
                    )
                    create_az_kv(chosen["full_name"])
                    kv_done_stacks.add(chosen["full_name"])
                    kv_exists[chosen["full_name"]] = True

                actions.insert(1, ("Create an Azure Key Vault", create_kv_for_selected_stack))

            msg("Menu:", COLOR_CYAN)
            for idx, (label, _) in enumerate(actions, start=1):
                msg(f"  {idx}) {label}", COLOR_CYAN)
            msg("  q) quit", COLOR_CYAN)
            msg("")

            try:
                choice = input(f"Select an option [1-{len(actions)}]: ").strip().lower()
            except EOFError:
                msg_stderr("Input closed; exiting.", COLOR_ORANGE)
                break

            if quit_input_detected(choice):
                # Quit from "no incomplete stacks" menu: show pulumi commands only if current stack is complete.
                if current_stack_full and summaries.get(current_stack_full, {}).get("status") == "complete":
                    msg(
                        "INFO : You can run: pulumi preview, pulumi up, pulumi stack output",
                        COLOR_CYAN,
                    )
                break

            if not choice.isdigit():
                msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                continue

            idx = int(choice)
            if idx < 1 or idx > len(actions):
                msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                continue

            label, func = actions[idx - 1]
            func()
            continue

        # ========== Menu B: at least one incomplete stack — focus one "active" stack ==========
        # There is at least one incomplete stack. Pick an active one.
        # If the current stack is already complete, prefer it so we can offer
        # peering/route and NSG rule update actions.
        active = None
        if current_stack_full and summaries.get(current_stack_full, {}).get("status") == "complete":
            active = next((s for s in stacks if s["full_name"] == current_stack_full), None)

        if active is None:
            if len(incomplete_stacks) == 1:
                active = incomplete_stacks[0]
            else:
                # Two or more incomplete stacks: let user choose which to work with.
                msg("Select which stack to work with:", COLOR_CYAN)
                for i, s in enumerate(incomplete_stacks, start=1):
                    msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
                msg("  q) quit", COLOR_CYAN)
                msg("")
                try:
                    choice = input(
                        f"Select stack [1-{len(incomplete_stacks)}]: "
                    ).strip().lower()
                except EOFError:
                    msg_stderr("Input closed; exiting.", COLOR_ORANGE)
                    break
                if quit_input_detected(choice):
                    break
                if not choice.isdigit():
                    msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                    continue
                idx = int(choice)
                if idx < 1 or idx > len(incomplete_stacks):
                    msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                    continue
                active = incomplete_stacks[idx - 1]

        active_name = active["full_name"]
        active_summary = summaries[active_name]
        has_kv_name = active_summary.get("has_kv_name", False)
        missing_required = get_missing_required_config(active["stack_file"])
        stack_variables_done = not missing_required
        kv_done = active_name in kv_done_stacks
        kv_already_exists = kv_exists.get(active_name, False)

        msg(f"Active stack: {active_name}", COLOR_CYAN)
        if stack_variables_done:
            msg("  - Stack variables: SET", COLOR_GREEN)
        else:
            msg("  - Stack variables: INCOMPLETE", COLOR_ORANGE)
        if has_kv_name:
            msg("  - Key Vault variable: SET", COLOR_GREEN)
        else:
            msg("  - Key Vault variable: NOT SET", COLOR_ORANGE)

        if azure_env and has_kv_name:
            if kv_done:
                msg("  - Azure Key Vault: CREATED (this session)", COLOR_GREEN)
            elif kv_already_exists:
                msg("  - Azure Key Vault: EXISTS", COLOR_GREEN)
            else:
                msg("  - Azure Key Vault: NOT READY", COLOR_ORANGE)
        elif not azure_env:
            msg("  - Azure Key Vault: N/A (no Azure provider detected)", COLOR_CYAN)
        else:
            msg("  - Azure Key Vault: BLOCKED (key_vault_name missing)", COLOR_ORANGE)

        # Actions depend on whether the active stack is actually complete (e.g. current stack complete but others not).
        actions: list[tuple[str, callable]] = []

        # Complete active stack: peerings/routes/NSG edit the YAML directly (no pulumi config set for whole objects).
        if active_summary.get("status") == "complete":
            # These actions modify nested objects (peerings + route_tables + hub_nsg_rules)
            # in the selected stack YAML.
            actions.append(
                (
                    "Add peering (and routes)",
                    lambda ast=active: add_peering_and_routes_to_stack(ast),
                )
            )
            actions.append(
                (
                    "Add hub NSG rule",
                    lambda ast=active: add_hub_nsg_rule_to_stack(ast),
                )
            )

        # Option to create Azure Key Vault (only when Azure is detected, key_vault_name is set,
        # and we haven't already created it in this session).
        if azure_env and has_kv_name and not kv_done and not kv_already_exists:
            actions.append(("Create an Azure Key Vault", lambda: create_az_kv(active_name)))

        # Set stack variables: seed from default_vars, then set any missing required (one at a time).
        if missing_required or not has_kv_name:
            actions.append(
                (
                    "Set stack variables",
                    lambda an=active_name, sf=active["stack_file"]: (
                        seed_default_vars(an),
                        run_set_required_variables(an, sf),
                    ),
                )
            )

        # Always allow creating a new stack.
        actions.append(("Create new stack", create_new_stack))

        # Check next on-prem network (only when at least one stack has cloud_network_space key set).
        local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
        if any(stack_has_cloud_network_space_key(s["stack_file"]) for s in local_stacks):
            actions.append(("Check for next available on-prem connecting network space", run_check_next_onprem_network))

        msg("Menu:", COLOR_CYAN)
        for idx, (label, _) in enumerate(actions, start=1):
            msg(f"  {idx}) {label}", COLOR_CYAN)
        msg("  q) quit", COLOR_CYAN)
        msg("")

        try:
            choice = input(f"Select an option [1-{len(actions)}]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; exiting.", COLOR_ORANGE)
            break

        if quit_input_detected(choice):
            # Only suggest pulumi preview/up when the active stack is complete (all required vars set).
            if active_summary.get("status") == "complete":
                stack_file = active["stack_file"]
                msg(
                    f"INFO : Stack configuration file '{stack_file}' is present on this machine.",
                    COLOR_CYAN,
                )
                msg("You can run:", COLOR_CYAN)
                msg("  pulumi preview", COLOR_CYAN)
                msg("  pulumi up", COLOR_CYAN)
                msg("  pulumi stack output", COLOR_CYAN)
            break

        if not choice.isdigit():
            msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
            continue

        idx = int(choice)
        if idx < 1 or idx > len(actions):
            msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
            continue

        label, func = actions[idx - 1]

        # Execute the selected action.
        before_kv_done = kv_done
        func()

        # If we just created a Key Vault for this stack, remember that in this session.
        if not before_kv_done and label.startswith("Create an Azure Key Vault"):
            kv_done_stacks.add(active_name)
            kv_exists[active_name] = True

# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def main() -> None:
    """Script entry: start the interactive menu (no CLI args)."""
    interactive_menu()


if __name__ == "__main__":
    main()
