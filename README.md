# Azure Spoke Network (Pulumi)

Pulumi (Python) project that deploys an **Azure spoke VNet**: networking resource group, a single **VNet/subnet**, classic **NSG** + optional **`nsg_rules`**, a **native route table** (UDRs toward the hub **Palo Alto** using **trust/untrust** next-hop private IPs from a **`StackReference`**), and optional **VNet peerings** to the hub. **`route_tables`**, **`nsg_rules`**, and **`peerings`** are **config-driven** from `Pulumi.<stack>.yaml`, following the same pattern as **azure-pa-hub-network**.

**Firewall next hops** are loaded via a **`StackReference`** to **azure-pa-hub-network**, which must export **`trust_nic_private_ip`** and **`untrust_nic_private_ip`**.

Optional **`cloud_network_space`** (`name` + `cidr`), aligned with the hub, supports **`get_next_onprem_net.py`** and the on-prem menu in **`stack_menu.py`**. This program does **not** deploy VMs; use another stack (e.g. **azure-dev-vms**) for compute.

---

## Repository layout

| Path | Purpose |
|------|---------|
| **`Pulumi.yaml`** | Project name (`azure-spoke-network`), runtime; must **not** set `virtualenv: venv` if you use the Docker helpers (they fail fast on that). |
| **`__main__.py`** | Pulumi entry program (resources + exports). |
| **`default_vars.yaml`** | Template of stack keys for **`stack_menu.py`**: **`__REQUIRED__`**, **`__OPTIONAL__`**, **`__SECRET__`**. Keys are merged into `Pulumi.<stack>.yaml` with the project namespace. See [Configuration](#configuration). |
| **`Pulumi.dev-eip.yaml`** | **Example** stack file in-repo (real naming/IDs may differ); illustrates `peerings`, **`route_tables`**, **`nsg_rules`**, `pa_hub_stack`, etc. Copy patterns for new stacks. Local working stacks are usually **gitignored** (`Pulumi.*.yaml`). |
| **`requirements.txt`** | Python deps: Pulumi, **pulumi-azure** + **pulumi-azure-native**, **azure-cli**, **azure-identity**, **PyYAML** (`import yaml`), **dpath**, **azure-mgmt-network**, **azure-keyvault-secrets** (shared/menu patterns). Do **not** add a PyPI package named `yaml` — use **`pyyaml`**. |
| **`Dockerfile`** | Image from **`pulumi/pulumi-python`**, installs **`requirements.txt`**, interactive bash. |
| **`docker_pulumi_shell.sh`** | Linux/macOS/WSL: build/run the dev container (see [Docker shell launchers](#docker-shell-launchers)). |
| **`win_docker_pulumi_shell.bat`** | Windows **cmd**: same behavior as the shell script. |
| **`stack_menu.py`** | Shared-style interactive menu: stack checklist, seed from **`default_vars.yaml`**, route/NSG/peering/on-prem helpers. **Key Vault is optional** for this project; **`create_keyvault.py`** is imported only if present beside this script (copy from **azure-pa-hub-network** if you need KV flows elsewhere). Spoke **`route_tables`** / **`nsg_rules`** defaults and route-name prefixes are defined in code (see [Recent changes](#recent-changes)). |
| **`get_next_onprem_net.py`** | CLI/module: next free subnet under **`cloud_network_space.cidr`** (Azure ARM scan). |
| **`Windows-Integration.md`** | Docker Desktop + WSL, Git, and `python` on Windows/WSL. |

**`.gitignore`** — typically **`Pulumi.*.yaml`** (except files you choose to commit, e.g. examples), `*.json`, **`__pycache__/`**. Create working configs with **`pulumi stack init`** / **`stack_menu.py`**, or copy from **`Pulumi.dev-eip.yaml`**.

---

## Prerequisites

- **Pulumi CLI** — [install](https://www.pulumi.com/docs/install/)
- **Azure CLI** — `az login`; subscription access for the spoke (and read access as needed for **`get_next_onprem_net.py`**)
- **Python 3** — for running scripts on the host, or use Docker (below)
- **Docker** (optional) — for **`./docker_pulumi_shell.sh`** / **`win_docker_pulumi_shell.bat`**
- **`PULUMI_ACCESS_TOKEN`** (or **`PULUMI_ENV_FILE`** containing it) — required to enter the container and for Pulumi Cloud backends

Install deps on the host if not using Docker:

```bash
pip install -r requirements.txt
```

---

## Docker shell launchers

Both **`docker_pulumi_shell.sh`** and **`win_docker_pulumi_shell.bat`**:

1. Require **`Pulumi.yaml`**, **`Dockerfile`**, and **`requirements.txt`** in the project directory.
2. Refuse to run if **`Pulumi.yaml`** sets `virtualenv: venv` (conflicts with the image).
3. Tag the image as **`pulumi/<folder-name>`** (folder name lowercased; non-alphanumeric → `-`). Example: repo folder **`azure-spoke-network`** → image **`pulumi/azure-spoke-network`**.
4. Mount the **current project directory** at **`/app`** in the container (`-w /app`).
5. Pass **Pulumi token** via **`PULUMI_ACCESS_TOKEN`** or **`PULUMI_ENV_FILE`** (file must contain a line `PULUMI_ACCESS_TOKEN=...`). With **`PULUMI_ENV_FILE`**, the container also gets **`--env-file`** (handy for extra env vars).
6. If **`../lib/requirements.txt`** exists, merge it into the build context and mount **`../lib`** → **`/app/lib`**.
7. If **`PULUMI_ENVIRONMENT=Development`**, create/use a **named Docker volume**, persist its id in **`.persistent_vol`**, and mount it at **`/persistent`** (Linux script and Windows batch).

### Linux / macOS / WSL — `docker_pulumi_shell.sh`

```bash
cd /path/to/azure-spoke-network
chmod +x docker_pulumi_shell.sh    # once

export PULUMI_ACCESS_TOKEN="pul-xxxxxxxx"
./docker_pulumi_shell.sh
```

**Token via file:**

```bash
export PULUMI_ENV_FILE="$HOME/.pulumi-env"
./docker_pulumi_shell.sh
```

**Build image only (no shell):**

```bash
./docker_pulumi_shell.sh --build-only
```

**Remove this project’s image** (removes containers using it; prompts unless `--yes`):

```bash
./docker_pulumi_shell.sh --destroy-image
./docker_pulumi_shell.sh --destroy-image --yes
```

**Help:**

```bash
./docker_pulumi_shell.sh --help
```

**Inside the container** (typical flow):

```bash
az login
pulumi stack select dev    # or: pulumi stack init org/dev
python stack_menu.py           # optional: seed / edit YAML-backed config
pulumi preview
pulumi up
```

### Windows — `win_docker_pulumi_shell.bat`

Run from **cmd** with Docker Desktop running. The batch file **`cd`s to its own directory**, so you can double-click it or run:

```bat
cd C:\path\to\azure-spoke-network
set PULUMI_ACCESS_TOKEN=pul-xxxxxxxx
win_docker_pulumi_shell.bat
```

Or with an env file:

```bat
set PULUMI_ENV_FILE=C:\Users\you\.pulumi-env
win_docker_pulumi_shell.bat
```

Same flags as the shell script:

```bat
win_docker_pulumi_shell.bat --build-only
win_docker_pulumi_shell.bat --destroy-image
win_docker_pulumi_shell.bat --destroy-image --yes
win_docker_pulumi_shell.bat --help
```

---

## Scripts and programs (reference)

### `stack_menu.py` — primary operator UI

Interactive menu from the **project root**. The same script is designed to run across related repos (**azure-spoke-network**, **azure-pa-hub-network**, **azure-dev-vms**, **azure-prod-vms**) with behavior that adapts to **`Pulumi.yaml`** `name`.

- **`create_keyvault.py`** is imported **only if** that file exists next to **`stack_menu.py`**. This spoke repo often omits it; Key Vault flows are **not required** for **azure-spoke-network** (the menu treats KV as optional for this project).

```bash
python stack_menu.py
# or, with shebang + chmod +x:
./stack_menu.py
```

| Area | Behavior |
|------|----------|
| **Checklist** | Discovers stacks (`pulumi stack ls --json` plus local `Pulumi.*.yaml`). Completeness is driven by **`default_vars.yaml`** merge. |
| **Key Vault** | For **`azure-spoke-network`**, Key Vault is **not** treated as a blocking requirement; other projects keep KV checklist behavior when **`key_vault_name`** is used. |
| **Seed / set variables** | Merges **`default_vars.yaml`** into **`Pulumi.<stack>.yaml`**. Walks missing keys; **special** keys get Azure-shaped templates (**`route_tables`**, **`peerings`**, **`cloud_network_space`**, **`nsg_rules`** / **`hub_nsg_rules`** depending on project). |
| **`route_tables`** | Submenu appends routes or loads **default templates**. For this spoke, defaults are **hard-coded** in **`stack_menu.py`**; route **`name`** fields are normalized using **`spoke_prefix`** from the stack YAML (e.g. `<spoke_prefix>-to-FW-Route1`). |
| **Peering + routes** | Append peering and matching routes using **`address_prefix_ref: peerings.<n>.cidr`** where applicable. |
| **NSG rules** | This project uses config key **`nsg_rules`** (not **`hub_nsg_rules`**). |
| **On-prem CIDR** | Runs **`get_next_onprem_net.py`** for stacks with **`cloud_network_space`**. |

---

### `get_next_onprem_net.py` — next on-prem subnet (Azure)

Purpose: suggest the **next available** subnet under your **`cloud_network_space.cidr`** that does not overlap existing Azure VNET prefixes in that space.

- Resolves stack from **`--stack`**, then **`PULUMI_STACK`**, then current **`pulumi stack`**.
- Reads **`cloud_network_space.cidr`** from `Pulumi.<stack>.yaml`.
- Enumerates subscriptions visible to **`az login`** and collects VNET address spaces.
- Supported masks: **`/24` `/25` `/26` `/27` `/28` `/29`**.

```bash
python get_next_onprem_net.py /28
python get_next_onprem_net.py /28 --stack dev
python get_next_onprem_net.py /28 --stack ORG/dev
```

Notes:

- Run from project root with **`az login`** completed.
- If no free subnet is available, the script exits non-zero with a message.

Unlike **azure-pa-hub-network**, this spoke **`__main__.py`** does **not** export a “next /24 spoke” output from this module during **`pulumi up`**.

---

### `__main__.py` — Pulumi program

Registered by **`Pulumi.yaml`**. Use **`pulumi preview`** / **`pulumi up`**, not `python __main__.py`.

- Loads **`network_resource_prefix`**, **`spoke_prefix`**, **`vnet1_cidr`**, **`on_prem_source_ip_range`**, **`pa_hub_stack`**, **`route_tables`** (required object), optional **`nsg_rules`**, optional **`peerings`**.
- **`StackReference`(`pa_hub_stack`)** → **`trust_nic_private_ip`**, **`untrust_nic_private_ip`** for UDR next hops where **`next_hop_ip_ref`** is **`trust_nic`** / **`untrust_nic`**.
- Builds NSG rules from **`nsg_rules`** when present (**`resolve_nsg_address`**: **`cfg.require`** on `*_ref` keys — use **top-level** keys such as **`vnet1_cidr`**, **`on_prem_source_ip_range`**).
- Builds routes from **`route_tables["VnetToFw"]`** with **`address_prefix`**, **`address_prefix_ref`** (dotted config paths like **`peerings.0.cidr`**), and **`next_hop_ip_ref`** when **`next_hop_type`** is **`VirtualAppliance`**.
- Peerings: **`use_remote_gateways=True`**, **`allow_gateway_transit=False`** on the spoke-side peering (see program for current flags).
- Creates: resource group, native route table, classic NSG, native VNet + subnet, subnet↔NSG and subnet↔route table associations, optional native **VirtualNetworkPeering** per **`peerings`** entry (**`name`**, **`remote_vnet_id`**, **`cidr`** required).
- **Exports:** **`{spoke_prefix}-VNET`** (CIDR string), and **`{peering_name} Peering CIDR`** for each configured peering.

**Dependencies:** Deploy **azure-pa-hub-network** (or ensure its stack outputs exist) **before** this stack so stack reference outputs resolve.

---

## Quick start (end-to-end)

```bash
export PULUMI_ACCESS_TOKEN=<token>
cd /path/to/azure-spoke-network
./docker_pulumi_shell.sh
az login
pulumi stack init dev          # or pulumi stack select <stack>
python stack_menu.py           # optional: seed config from default_vars.yaml / edit routes & peerings
pulumi preview
pulumi up
```

**Destroy:**

```bash
pulumi destroy
```

---

## Configuration

### `default_vars.yaml`

This file is the **single source of truth** for what the menu treats as required vs optional when seeding a stack. It is aligned with **`__main__.py`**:

| Key | In `__main__.py` |
|-----|------------------|
| **`network_resource_prefix`**, **`spoke_prefix`**, **`vnet1_cidr`**, **`on_prem_source_ip_range`**, **`pa_hub_stack`**, **`route_tables`** | Required |
| **`nsg_rules`**, **`peerings`**, **`cloud_network_space`** | Optional |
| Azure **`subscriptionId`**, **`tenantId`**, **`azure-native:location`** | Required (providers) |

Legacy keys such as **`rg_prefix`** or **`vm1`** are **not** used by this spoke program and are **not** listed in **`default_vars.yaml`**.

### `Pulumi.<stack>.yaml`

Stack config. Prefix keys with **`azure-spoke-network:`** (or your **`Pulumi.yaml`** `name` if you change it).

| Key / object | Role |
|--------------|------|
| **`azure:subscriptionId`**, **`azure:tenantId`**, **`azure-native:location`** | Providers |
| **`network_resource_prefix`** | RG and route table naming (`{prefix}-Networking`, `{prefix}-to-FW`) |
| **`spoke_prefix`** | VNet and subnet names (`{prefix}-VNET`, `{prefix}-subnet1`); also used by **`stack_menu.py`** when generating default **route** names |
| **`vnet1_cidr`** | Spoke VNet / subnet CIDR (reference as **`vnet1_cidr`** in NSG `*_ref` fields) |
| **`on_prem_source_ip_range`** | Scalar for routes/NSG refs |
| **`pa_hub_stack`** | Fully qualified hub stack (e.g. **`ORG/azure-pa-hub-network/dev`**) |
| **`route_tables`** | Object; **`__main__.py`** uses **`VnetToFw`** (list of routes) |
| **`nsg_rules`** | List of rules for classic NSG |
| **`peerings`** | Optional list of **`{ name, remote_vnet_id, cidr }`** |
| **`cloud_network_space`** | Optional **`{ name, cidr }`** for subnet calculator / menu |

See **`Pulumi.dev-eip.yaml`** for a full in-repo example.

---

## Hub ↔ spoke peering

For **Connected** state, **both** hub and spoke need a peering resource. On a clean topology, **either** side can be created first. Keep **`peerings[].remote_vnet_id`** aligned with the real spoke VNet resource ID, and hub **`peerings`** aligned with the same.

If you remove a peering in **Azure** outside Pulumi, reconcile **hub** state (**`pulumi refresh`** or **`pulumi state delete`** on the peering URN) before the next **`pulumi up`** — see **azure-pa-hub-network** README.

---

## Recent changes (this project)

- **`default_vars.yaml`**: Required keys now match **`__main__.py`** (**`network_resource_prefix`**, **`route_tables`**, etc.); removed unused **`rg_prefix`** / **`vm1`** placeholders; documented optional **`peerings`**, **`nsg_rules`**, **`cloud_network_space`**.
- **`stack_menu.py`**: Optional Key Vault for **azure-spoke-network**; optional import of **`create_keyvault.py`**; project-specific default templates for **`route_tables`** / **`nsg_rules`** (and hub-style constants when the same script is used in **azure-pa-hub-network**); **`spoke_prefix`** drives default route **name** generation for spoke stacks.
- **`requirements.txt`**: Use **`pyyaml`** for `import yaml` (do not add a **`yaml`** package line — it is not a valid PyPI name).
- **Example stack**: **`Pulumi.dev-eip.yaml`** documents a realistic layout; adjust IDs and names for your environment.

---

## Windows, WSL, and Docker

Use **Docker Desktop** (WSL 2 backend) and enable **WSL integration** for Ubuntu so `docker` in WSL uses the same engine as Windows. For Git line-ending / `fileMode` issues on shared Windows drives, **`python` → Python 3** on Ubuntu, and a command cheat sheet, see **`Windows-Integration.md`**.

| Task | Command / location |
|------|---------------------|
| List WSL distros | `wsl -l -v` |
| Default distro | `wsl --set-default Ubuntu` |
| Docker ↔ WSL | Docker Desktop → Settings → Resources → WSL Integration |
| Git (repo on Windows drive) | From WSL in repo: `git config core.fileMode false` and optionally `git config core.autocrlf input` |
| `python` = Python 3 (Ubuntu) | `sudo apt install -y python-is-python3` |

---

## Notes

- **Project name** in **`Pulumi.yaml`** drives the config key prefix in stack YAML (`azure-spoke-network` by default).
- **No `pulumi_docker.sh`** in this repo — use **`docker_pulumi_shell.sh`** or **`win_docker_pulumi_shell.bat`**.
- **`__main__.py`** imports Azure Key Vault / storage modules that are **not** all used by the minimal spoke resources; dependencies and **`stack_menu.py`** follow shared patterns across repos.
- **Developed by** Andrew Tamagni (see file headers for history).

---

## AI Assistance Disclosure

This README was drafted with AI assistance. Some portions of the repository were developed with assistance from Cursor AI. The specific underlying model can vary by session and configuration. AI assistance was used for parts of code generation and documentation, and all code/documentation have been reviewed, verified, and refined by humans for quality and accuracy.
