# Azure Spoke Network (Pulumi)

Pulumi project for a **spoke VNet**: resource group, **VNet/subnet**, classic **NSG**, **route table** (UDRs via **`pa_hub_stack`** to Palo Alto **trust/untrust** IPs), and **at least one VNet peering** to the hub. **`peerings`** must be a non-empty list (unlike the hub project, where **`peerings`** are optional). Does not deploy VMs — use **azure-vms** for compute.

Optional **`cloud_network_space`** enables **`get_next_onprem_net.py`** and the on-prem menu entry.

---

## Run with Docker

You need [Docker](https://docs.docker.com/get-docker/), a [Pulumi access token](https://www.pulumi.com/docs/pulumi-cloud/access-tokens/) (or `PULUMI_ENV_FILE` with `PULUMI_ACCESS_TOKEN=...`), and this repo’s **`Pulumi.yaml`**, **`Dockerfile`**, and **`requirements.txt`**. Do **not** set `virtualenv: venv` in **`Pulumi.yaml`** — the helper scripts refuse to run if it is set.

**Set the token on the host before** you run **`docker_pulumi_shell.sh`** or **`win_docker_pulumi_shell.bat`**, so the shell script can pass **`PULUMI_ACCESS_TOKEN`** into the container. Replace the placeholder with your real token.

PowerShell:

```powershell
$env:PULUMI_ACCESS_TOKEN = "pul-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

Bash (Linux, macOS, WSL):

```bash
export PULUMI_ACCESS_TOKEN=pul-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**`docker_pulumi_shell.sh`** passes **`HOST_UID`** / **`HOST_GID`** from your Linux or macOS user into the container so **`stack_menu.py`** can **`chown`** new **`Pulumi.<stack>.yaml`** files on the bind-mounted repo to you (and set mode **`0644`**). On Windows **cmd**, set `set HOST_UID=...` and `set HOST_GID=...` before **`win_docker_pulumi_shell.bat`** if your stack files end up owned by root, or fix ownership once with **`sudo chown`** on WSL.

**Linux / macOS / WSL** — build the image and open a shell in `/app`:

```bash
cd /path/to/azure-spoke-network
chmod +x docker_pulumi_shell.sh    # once
export PULUMI_ACCESS_TOKEN="pul-xxxx"
./docker_pulumi_shell.sh
```

**Examples:** build only — `./docker_pulumi_shell.sh --build-only`. Token in a file — `export PULUMI_ENV_FILE="$HOME/.pulumi-env"` then run the script. All flags — `./docker_pulumi_shell.sh --help`.

The image is tagged **`pulumi/azure-spoke-network`**.

**Windows (PowerShell)** — from the repo directory with Docker Desktop running:

```bat
$env:PULUMI_ACCESS_TOKEN = "pul-xxxx"
win_docker_pulumi_shell.bat
```

For WSL, Git, and line endings on Windows drives, see **`Windows-Integration.md`**. To run without Docker: `pip install -r requirements.txt` on the host.

---

## Create a new stack

From the repo root, initialize and select a stack with the Pulumi CLI (Docker shell or host with **`pulumi`** / **`az`** installed). Then fill in **`Pulumi.<stack>.yaml`**. **`python stack_menu.py`** is the recommended path: it compares your stack file to the template, merges missing keys where safe, and walks prompts for incomplete values. You can instead copy **`Pulumi.sample.yaml`** by hand and edit.

**Prerequisite:** deploy **azure-pa-hub-network** first, or point **`pa_hub_stack`** at a hub stack that already exports **`trust_nic_private_ip`** and **`untrust_nic_private_ip`** (used as next-hop references in routes).

### Menu seeding (this project)

| Project (`Pulumi.yaml` `name`) | `stack_menu.py` checklist / seed |
|--------------------------------|-----------------------------------|
| **azure-spoke-network** (this repo) | **`Pulumi.sample.yaml`** |

### What `stack_menu.py` does here (this repo)

- Treats **`Pulumi.sample.yaml`** as the shape and placeholder reference: finds missing or sample-style values in **`Pulumi.<stack>.yaml`** and can merge in missing keys without overwriting what you already set.
- **Create new stack** runs a spoke-specific wizard (region, **`network_resource_prefix`** / **`spoke_prefix`**, real **`pa_hub_stack`** backend name, **`vnet1_cidr`**, **`on_prem_source_ip_range`**, peering entries from the sample shape, and related fields); applies **`azure:subscriptionId`** / **`azure:tenantId`** from **`az account show`** when available.
- For **complete** stacks, optional actions include adding **spoke NSG** rules, **UDR** routes on **`VnetToFw`**, and **spoke→hub peering** plus matching routes (menus are network-focused; **Key Vault** is not required on the checklist for this project).
- If **`cloud_network_space`** is set, the on-prem helper / **`get_next_onprem_net.py`** workflow is available from the menu.

### Configure the stack

- **Recommended:** `python stack_menu.py` — checklist, merge from sample, guided **Set stack variables** / **Create new stack**.
- **Manual:** copy **`Pulumi.sample.yaml`** to **`Pulumi.<stack>.yaml`** and replace every placeholder (subscription, tenant, ARM IDs, hub stack name, etc.).

**Required for `pulumi up`:** non-empty **`peerings`**, **`route_tables`** with at least **`VnetToFw`**, **`pa_hub_stack`**, **`network_resource_prefix`**, **`spoke_prefix`**, **`vnet1_cidr`**, **`on_prem_source_ip_range`**, and Azure provider keys. **Optional:** **`nsg_rules`**, **`cloud_network_space`**.

**Example:**

```bash
az login
pulumi stack init dev
pulumi stack select dev
python stack_menu.py    # checklist, merge, guided fixes
pulumi preview && pulumi up
```

---

## Deploy and destroy

```bash
pulumi preview
pulumi up
pulumi destroy
```

---

## Maintaining versions

When you refresh this project’s tooling, update these together so previews and deploys stay consistent:

- **`Dockerfile`**: bump the **`pulumi/pulumi-python`** image tag (currently **`pulumi/pulumi-python:3.220.0`**) to a current release from [Docker Hub — `pulumi/pulumi-python`](https://hub.docker.com/r/pulumi/pulumi-python/tags) so the image’s bundled Pulumi CLI matches your expectations.
- **`requirements.txt`**: review and update pinned **`pulumi`** and provider packages (for example **`pulumi-azure`**, **`pulumi-azure-native`**, and any other Python dependencies) so they remain compatible with that base image; then rebuild the Docker image (for example `./docker_pulumi_shell.sh --build-only` or your usual **`docker build`**).

If you run Pulumi on the host instead of Docker, align the installed **`pulumi`** CLI and **`pip install -r requirements.txt`** with the same versions where practical.

---

## Peering and state

**Hub-side peering:** Azure VNet peering uses two one-way links. This project creates only the outbound peerings from **this** stack’s virtual networks toward the remote VNet (for example the hub). For each pair to reach **Connected**, the **hub** stack (**azure-pa-hub-network**) must also define the matching return peering in its **`peerings`** configuration and you must run **`pulumi up`** there, or you must create that reverse link manually in Azure. Either direction may be created first.

Here, the outbound leg is the spoke→hub peering: **`remote_vnet_id`** in **`peerings`** is the hub VNet; the hub’s matching **`peerings`** entry must target this spoke’s VNet resource ID.

If you delete a peering in Azure outside Pulumi, reconcile with **`pulumi refresh`** or state edits before the next **`pulumi up`** (see **azure-pa-hub-network** README).

---

## Project layout (quick reference)

| Path | Role |
|------|------|
| **`__main__.py`** | Spoke VNet, routes, NSG, peerings; **`StackReference`** to hub for next-hop IPs. |
| **`stack_menu.py`** | Checklist + seed from **`Pulumi.sample.yaml`**. Key Vault is **optional** here. |
| **`Pulumi.sample.yaml`** | Full template (routes, **required** **`peerings`**, etc.). |
| **`default_vars.yaml`** | **`__REQUIRED__` / `__OPTIONAL__`** sketch; keep aligned with **`__main__.py`**. |
| **`get_next_onprem_net.py`** | Next free subnet under **`cloud_network_space.cidr`** (masks **`/24`–`/29`**). |

Use **`pyyaml`** in **`requirements.txt`** for `import yaml` (not a PyPI package named `yaml`).

---

## Developed By

Andrew Tamagni (see file headers for history).

---

## AI Assistance Disclosure

Portions of this repository and documentation were developed with assistance from Cursor AI and have been reviewed by humans.
