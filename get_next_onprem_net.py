#!/usr/bin/env python3
# Get the next available on-prem connecting subnet from the stack's cloud_network_space (Azure).
# Run from the project root. You must be logged into Azure CLI with valid credentials.
#
# Usage: python get_next_onprem_net.py MASK [--stack STACK]
#   MASK: /24, /25, /26, /27, /28, or /29
#   --stack: optional; required if more than one stack has a local config. Use 'dev' or 'org/dev'.
#
# Developed 12/2021 by Andrew Tamagni
# Updated 10/22/2025: Pulumi stack support
# Updated 03/2026: stack based on --stack argument

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import yaml

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

CIDR_CHOICES = ["/24", "/25", "/26", "/27", "/28", "/29"]

# ANSI color codes (disabled when stdout is not a TTY). Same scheme as create_keyvault.py / stack_menu.py.
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_CYAN = "\033[36m"
COLOR_ORANGE = "\033[33m"
COLOR_RED = "\033[31m"


def color_enabled() -> bool:
    """Return True if we should emit ANSI colors (terminal supports it)."""
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False


def msg(text: str, color_code: str | None = None) -> None:
    """Print message to stdout with optional color."""
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


def fail(text: str) -> None:
    """Print an error message and exit with status 1."""
    msg_stderr(f"ERROR : {text}", COLOR_RED)
    raise SystemExit(1)


# -----------------------------------------------------------------------------
# Project and stack helpers
# -----------------------------------------------------------------------------


def get_project_name() -> str:
    """Load project name from Pulumi.yaml in cwd."""
    path = "Pulumi.yaml"
    if not os.path.isfile(path):
        fail(f"Not a Pulumi project directory: {path} not found.")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    name = (data or {}).get("name")
    if not name:
        fail(f"{path} is missing a 'name' field.")
    return str(name).strip()


def discover_local_stacks() -> list[dict]:
    """
    Discover stacks that have a local Pulumi.<stack>.yaml file.
    Returns list of dicts: full_name, basename, stack_file.
    """
    # Keep the committed example stack config (Pulumi.sample.yaml) out of discovery.
    # Treat it as documentation, not a real stack to operate on.
    SAMPLE_STACK_BASENAME = "sample"

    stacks = []
    # Prefer Pulumi CLI so we get org/stack names.
    try:
        result = subprocess.run(
            ["pulumi", "stack", "ls", "--json"],
            check=True,
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
        )
        data = json.loads(result.stdout or "[]")
        for entry in data:
            full_name = (entry.get("name") or "").strip()
            if not full_name:
                continue
            basename = full_name.split("/")[-1]
            if basename == SAMPLE_STACK_BASENAME:
                continue
            stack_file = f"Pulumi.{basename}.yaml"
            if os.path.isfile(stack_file):
                stacks.append({"full_name": full_name, "basename": basename, "stack_file": stack_file})
    except Exception:
        pass
    if not stacks:
        # Fallback: local files only.
        for fname in sorted(os.listdir(".")):
            if not fname.startswith("Pulumi.") or not fname.endswith(".yaml") or fname == "Pulumi.yaml":
                continue
            basename = fname.replace("Pulumi.", "").replace(".yaml", "")
            if basename == SAMPLE_STACK_BASENAME:
                continue
            stacks.append({"full_name": basename, "basename": basename, "stack_file": fname})
    return stacks


def load_cloud_network_space(stack_file: str, project: str) -> dict | None:
    """Load cloud_network_space (name, cidr) from a stack config file. Returns None if missing/invalid."""
    if not os.path.isfile(stack_file):
        return None
    with open(stack_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    config = data.get("config") or {}
    key = f"{project}:cloud_network_space"
    value = config.get(key) or config.get("cloud_network_space")
    if not isinstance(value, dict):
        return None
    name = (value.get("name") or "").strip()
    cidr = (value.get("cidr") or "").strip()
    if name and cidr:
        return {"name": name, "cidr": cidr}
    return None


def resolve_stack(stacks: list[dict], stack_arg: str | None) -> dict:
    """
    Resolve which stack to use. If stack_arg is None, use the only stack (or fail).
    stack_arg can be full name (org/dev) or basename (dev).
    """
    if not stacks:
        fail("No Pulumi stacks found. Create a stack and ensure Pulumi.<stack>.yaml exists in this directory.")
    if len(stacks) == 1:
        if stack_arg and stack_arg != stacks[0]["full_name"] and stack_arg != stacks[0]["basename"]:
            fail(f"Only one stack is available ({stacks[0]['full_name']}); --stack must match it.")
        return stacks[0]
    # Multiple stacks: --stack required.
    if not stack_arg:
        msg_stderr("More than one stack has a local config. Specify which stack with --stack.", COLOR_ORANGE)
        msg_stderr("Example: --stack dev  or  --stack org/dev", COLOR_ORANGE)
        fail("Missing --stack argument.")
    stack_arg = stack_arg.strip()
    for s in stacks:
        if stack_arg == s["full_name"] or stack_arg == s["basename"]:
            return s
    fail(f"No stack matching {stack_arg!r}. Available: {', '.join(s['full_name'] for s in stacks)}")


# -----------------------------------------------------------------------------
# Core logic (Azure only; tied to stack cloud_network_space)
# -----------------------------------------------------------------------------

result = None


def get_available_subnets(address_space: str, existing_vnets: str, mask_bits: int) -> ipaddress.IPv4Network:
    """Return the first available subnet of the given size in address_space excluding existing_vnets."""
    global result
    existing_list = existing_vnets.split(",") if existing_vnets else []
    possible = list(ipaddress.ip_network(address_space).subnets(new_prefix=mask_bits))
    address_space_ips = set(ipaddress.ip_network(address_space))

    for vnet in existing_list:
        if not vnet.strip():
            continue
        for ip in ipaddress.ip_network(vnet):
            address_space_ips.discard(ip)

    for candidate in possible:
        hosts = list(ipaddress.ip_network(candidate).hosts())
        if all(ip in address_space_ips for ip in hosts):
            result = ipaddress.ip_network(candidate)
            return result

    fail(f"There are no more /{mask_bits} networks available in the configured address space.")


def get_azure_onprem_vnets(azure_address_space: str, mask_bits: int) -> ipaddress.IPv4Network:
    """Query Azure for VNETs in the address space and return the next available subnet."""
    from azure.identity import AzureCliCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.resource import SubscriptionClient

    credential = AzureCliCredential()
    onprem_vnets = []
    sub_client = SubscriptionClient(credential=credential)
    target = ipaddress.ip_network(azure_address_space)

    for subscription in sub_client.subscriptions.list():
        res_client = ResourceManagementClient(credential, subscription.subscription_id)
        net_client = NetworkManagementClient(credential, subscription.subscription_id)
        for group in res_client.resource_groups.list():
            for resource in res_client.resources.list_by_resource_group(group.name):
                if resource.type != "Microsoft.Network/virtualNetworks":
                    continue
                vnet = net_client.virtual_networks.get(group.name, resource.name)
                for prefix in vnet.address_space.address_prefixes:
                    if ipaddress.ip_network(prefix).subnet_of(target):
                        onprem_vnets.append(prefix)

    existing = ",".join(onprem_vnets)
    return get_available_subnets(azure_address_space, existing, mask_bits)


# -----------------------------------------------------------------------------
# CLI and main entry
# -----------------------------------------------------------------------------


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Get next available on-prem connecting subnet from the stack's cloud_network_space (Azure).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "mask",
        metavar="MASK",
        choices=CIDR_CHOICES,
        help="Subnet mask: /24, /25, /26, /27, /28, or /29",
    )
    parser.add_argument(
        "--stack",
        metavar="STACK",
        default=None,
        help="Stack to use (e.g. dev or org/dev). Required if more than one stack has a local config.",
    )
    return parser.parse_args()


def mask_to_int(mask: str) -> int:
    """Convert /24 -> 24 etc."""
    if mask in CIDR_CHOICES:
        return int(mask.lstrip("/"))
    fail(f"Invalid mask {mask!r}. Use one of: {', '.join(CIDR_CHOICES)}")


def main(MaskBitSize: str, stack_identifier: str | None = None):
    """
    Get the next available on-prem subnet for the stack's cloud_network_space.
    When called from __main__.py (Pulumi), stack_identifier is None and config is taken from
    PULUMI_STACK + stack file or pulumi.Config(). When run as CLI, the caller resolves stack
    and passes stack_identifier (full name) and loads config from file; this path is used
    only for the return value from __main__.py.
    """
    global result
    result = None
    mask_bits = mask_to_int(MaskBitSize)

    # Resolve config: from stack file (when stack_identifier or PULUMI_STACK set) or from Pulumi config.
    cloud_network_space = None
    if stack_identifier:
        project = get_project_name()
        stack_basename = stack_identifier.split("/")[-1]
        stack_file = f"Pulumi.{stack_basename}.yaml"
        cloud_network_space = load_cloud_network_space(stack_file, project)
    else:
        # PULUMI_STACK from env (when run under Pulumi or from stack_menu)
        stack_env = os.environ.get("PULUMI_STACK", "").strip()
        if stack_env:
            project = get_project_name()
            stack_basename = stack_env.split("/")[-1]
            stack_file = f"Pulumi.{stack_basename}.yaml"
            cloud_network_space = load_cloud_network_space(stack_file, project)
        else:
            try:
                import pulumi
                cfg = pulumi.Config()
                # Nested object must be read with get_object(), not get()
                cloud_network_space = cfg.get_object("cloud_network_space")
            except Exception:
                pass

    if not cloud_network_space or not isinstance(cloud_network_space, dict):
        fail("cloud_network_space is not set for this stack. Add to Pulumi.<stack>.yaml:\n  cloud_network_space:\n    name: azure_test\n    cidr: 10.100.0.0/20")

    cidr = (cloud_network_space.get("cidr") or "").strip()
    if not cidr:
        fail("cloud_network_space must have a 'cidr' field in Pulumi config.")

    get_azure_onprem_vnets(cidr, mask_bits)
    return result


if __name__ == "__main__":
    args = parse_arguments()
    project = get_project_name()
    stacks = discover_local_stacks()
    chosen = resolve_stack(stacks, args.stack)
    space = load_cloud_network_space(chosen["stack_file"], project)
    if not space:
        fail(f"cloud_network_space (name, cidr) is not set in {chosen['stack_file']}. Add it to use this script.")
    mask_bits = mask_to_int(args.mask)
    msg(f"Stack: {chosen['full_name']}  mask: {args.mask}  address space: {space['cidr']}", COLOR_CYAN)
    try:
        out = main(args.mask, stack_identifier=chosen["full_name"])
        msg(str(out), COLOR_GREEN)
    except SystemExit:
        raise
    except Exception as e:
        msg_stderr(str(e), COLOR_RED)
        raise SystemExit(1)
