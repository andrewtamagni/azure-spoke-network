# azure-spoke-network

This repository contains a Pulumi-based Python project that provisions Azure spoke network infrastructure. It uses Pulumi, Azure SDKs, and supporting scripts to build:

- Resource groups for networking and VM resources
- Virtual networks and subnets with custom addressing
- Network security groups with security rules
- Route tables for traffic routing through firewall
- VNET peering to hub network for hybrid connectivity
- A test Ubuntu VM for validation

The environment is designed to support connectivity with on-premises networks and hub networks, and includes tooling to calculate available subnet ranges across cloud and on-prem address spaces.

---

## Repository Highlights

### `__main__.py`
Main Pulumi program that defines the Azure spoke network resources.  
Key features:
- Defines spoke network topology with custom VNET and subnets.  
- Creates resource groups for networking and VM resources.  
- Configures route tables for traffic routing through firewall.  
- Sets up network security groups with security rules.  
- Establishes VNET peering to hub network for hybrid connectivity.  
- Deploys a test Ubuntu VM for validation.  
- Exports useful outputs (IPs, hostnames, subnet ranges).  

### `get_next_onprem_net.py`
Helper script to calculate the next available subnet range for cloud VNETs that peer with on-premises networks.  
- Supports Azure (production and test), with placeholders for AWS and GCP.  
- Uses Azure SDKs (`azure-identity`, `azure.mgmt.network`) to fetch deployed VNETs.  
- Returns the first available CIDR block of the requested size.  

Example:
```bash
python get_next_onprem_net.py /28
```

### `set_default_vars.py`
Initializes Pulumi stack configuration from `default_vars.yaml`.  
- Ensures required/secret/optional values are clearly flagged.  
- Writes config into `Pulumi.<stack>.yaml`.  

### Docker & Shell Scripts
- **`Dockerfile`**: Defines a container image with Python, Pulumi, and Azure CLI.  
- **`build_image.sh`**: Builds the Pulumi Docker image locally.  
- **`pulumi_shell.sh`**: Runs a shell inside the Pulumi container, providing a consistent environment with Pulumi and Azure CLI preinstalled.

**Note:** The Docker files referenced here were shared and not entirely written by me. They are maintained in a private repository and are available by request.  

---

## Setup

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/)  
- [Pulumi CLI](https://www.pulumi.com/docs/install/)  
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)  

Install Python requirements if running outside Docker:
```bash
pip install -r requirements.txt
```

---

## Usage

### Initial Setup
```bash
export PULUMI_ACCESS_TOKEN=<Insert Pulumi Access Token>
./pulumi_shell.sh
az login
pulumi stack init
python set_default_vars.py
```

### Configure Stack (DEV Example)
```bash
pulumi config set --path azure:subscriptionId xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx 
pulumi config set --path azure:tenantId xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
pulumi config set --path azure-native:location WestUS
pulumi config set rg_prefix DEV
pulumi config set --path vnet1.cidr xx.xx.xx.xx/24
pulumi config set --path vnet1.prefix WEST-1
pulumi config set --path vm1.name aztest1
pulumi config set --path vm1.admin_pw '<insert keeper pw: https://keepersecurity.com/vault/#detail/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx>' --secret
pulumi config set trust_network_interface xx.xx.xx.xx
pulumi config set cloud_network_env azure-test
pulumi config set --path peerings.hub_vnet_id /subscriptions/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/resourceGroups/DEV-Networking/providers/Microsoft.Network/virtualNetworks/DEV-HUB
pulumi config set --path peerings.hub_cidr xx.xx.xx.xx/22
```

### Deploy
```bash
pulumi up
```

### Destroy
```bash
pulumi destroy
```

---

## Notes
- `.gitignore` excludes sensitive Pulumi stack files and secrets (`Pulumi.*.yaml`).  
- Secrets (passwords, keys) should be stored in Keeper and set via `pulumi config set --secret`.  
- Outputs include IPs, hostnames, and calculated subnet ranges to simplify downstream automation.

---

## Third-Party Dependencies

This project uses the following third-party open-source libraries and tools:

- **Pulumi** (Apache-2.0 License) - Infrastructure as Code framework
- **Pulumi Azure Native** (Apache-2.0 License) - Azure provider for Pulumi
- **Pulumi Azure** (Apache-2.0 License) - Azure Classic provider for Pulumi
- **Azure Identity** (MIT License) - Azure authentication library
- **Azure CLI** (MIT License) - Azure command-line interface
- **Azure Management Network** (MIT License) - Azure network management SDK
- **dpath** (BSD-2-Clause License) - Dictionary path utilities
- **PyYAML** (MIT License) - YAML parser and emitter

All dependencies are listed in `requirements.txt`. Please refer to each library's license for specific terms and conditions.