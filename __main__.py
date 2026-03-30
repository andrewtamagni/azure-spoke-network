"""An Azure RM Python Pulumi program"""
# Developed on 12/2021 by Andrew Tamagni
# Updated 10/22/2025 by Andrew Tamagni - Converted Windows VM to Ubuntu VM.  Added Peering to Hub VNET.
# Updated: Route tables, NSG rules, peerings from Pulumi.<stack>.yaml (config-driven, like azure-core-infrastructure).

import pulumi
from pulumi import StackReference
from pulumi_azure_native import storage
from pulumi_azure_native import resources
import pulumi_azure_native as azure_native
import pulumi_azure_native.network as azure_native_networking
import pulumi_azure as azure_classic
from azure.identity import AzureCliCredential
from azure.keyvault.secrets import SecretClient
from pulumi_azure_native.network import network_security_group
from pulumi_azure_native.network.network_security_group import NetworkSecurityGroup

pulumi.log.info("Deploying Resources")

def build_hub_nsg_rules(rules_list: list, cfg) -> list:
    """Convert `hub_nsg_rules` config entries into NSG security rule arguments.
    Keeps NSG rule definitions declarative in `Pulumi.<stack>.yaml`.
    """
    out = []
    for rule in rules_list:
        source = resolve_nsg_address(rule, "source_address_prefix", cfg)
        dest = resolve_nsg_address(rule, "destination_address_prefix", cfg)
        out.append(azure_classic.network.NetworkSecurityGroupSecurityRuleArgs(
            name=rule["name"],
            description=rule.get("description", "Rule"),
            protocol=rule.get("protocol", "*"),
            source_port_range=rule.get("source_port_range", "*"),
            destination_port_range=rule.get("destination_port_range", "*"),
            source_address_prefix=source,
            destination_address_prefix=dest,
            access=rule["access"],
            priority=rule["priority"],
            direction=rule["direction"],
        ))
    return out

def build_routes(route_list, network_resource_prefix, address_refs, trust_nic_ip, untrust_nic_ip, cfg):
    """Build `RouteArgs` objects from stack configuration.
    Handles:
    - Naming using either an explicit `name` or a `name_suffix` appended to the
      shared `network_resource_prefix`.
    - Destination prefixes via `resolve_address_prefix`, which allows literal
      CIDRs, computed refs, or dotted config paths.
    - Next-hop IPs via `next_hop_ip_ref` ('trust_nic' / 'untrust_nic') so that
      routes automatically follow the deployed firewall NIC addresses.
    """
    out = []
    for route in route_list:
        name = route.get("name") or (str(network_resource_prefix) + route["name_suffix"])
        address_prefix = resolve_address_prefix(route, address_refs, cfg)
        next_hop_type = route["next_hop_type"]
        next_hop_ip_address = None
        if route.get("next_hop_ip_ref") == "trust_nic":
            next_hop_ip_address = trust_nic_ip
        elif route.get("next_hop_ip_ref") == "untrust_nic":
            next_hop_ip_address = untrust_nic_ip
        kwargs = dict(name=name, address_prefix=address_prefix, next_hop_type=next_hop_type)
        if next_hop_ip_address is not None:
            kwargs["next_hop_ip_address"] = next_hop_ip_address
        out.append(azure_native.network.RouteArgs(**kwargs))
    return out

def resolve_address_prefix(route_def, address_refs, cfg):
    """Resolve a route's address prefix from literal, computed ref, or config path."""
    if "address_prefix" in route_def:
        return route_def["address_prefix"]
    ref = route_def["address_prefix_ref"]
    if ref in address_refs:
        return address_refs[ref]
    return resolve_config_path(cfg, ref)

def resolve_config_path(cfg, path):
    """Resolve a dotted config path from Pulumi stack YAML."""
    segments = path.split(".")
    key = segments[0]
    if len(segments) == 1:
        return cfg.require(key)
    obj = cfg.require_object(key)
    for segment in segments[1:]:
        if segment.isdigit():
            obj = obj[int(segment)]
        else:
            obj = obj[segment]
    return obj

def resolve_nsg_address(rule: dict, prefix_key: str, cfg) -> str:
    """Resolve source or destination address for an NSG rule.
    If `<prefix_key>_ref` is present (e.g. `source_address_prefix_ref`), the
    value is treated as a Pulumi config key and loaded via `cfg.require`.
    Otherwise the literal `<prefix_key>` value is returned.
    """
    ref_key = prefix_key + "_ref"
    if ref_key in rule:
        return cfg.require(rule[ref_key])
    return rule[prefix_key]
######################## Stack Configuration ########################
# Grab variables from Pulumi.<stack>.yaml
cfg                          = pulumi.Config()
cfg_az                       = pulumi.Config("azure-native")
network_resource_prefix      = cfg.require("network_resource_prefix")
spoke_prefix                 = cfg.require("spoke_prefix")
vnet1_cidr                   = cfg.require("vnet1_cidr")
on_prem_source_ip_range      = cfg.require("on_prem_source_ip_range")
pa_hub_stack                 = cfg.require("pa_hub_stack")
config_route_tables          = cfg.require_object("route_tables")

# Additional Hub NSG rules optional
try:
    raw_nsg_rules = cfg.get_object("nsg_rules")
    config_nsg_rules = raw_nsg_rules if isinstance(raw_nsg_rules, list) else []
except Exception:
    config_nsg_rules = []

# Stack reference to azure-pa-hub-network (e.g. org/azure-pa-hub-network/dev)
hub_stack = StackReference(pa_hub_stack)
trust_nic_private_ip = hub_stack.get_output("trust_nic_private_ip")
untrust_nic_private_ip = hub_stack.get_output("untrust_nic_private_ip")

######################## Peerings ########################
try:
    raw_peerings = cfg.get_object("peerings")
    config_peerings = raw_peerings if isinstance(raw_peerings, list) else []
except Exception:
    config_peerings = []

# Create an Azure Resource Group
networking_resource_group= resources.ResourceGroup(str(network_resource_prefix) + "-Networking",
    resource_group_name=str(network_resource_prefix) + "-Networking")

# Create Route Table (routes from route_tables.VnetToFw in Pulumi.<stack>.yaml)
vnet_to_fw_route_table = azure_native.network.RouteTable(str(network_resource_prefix) + "-to-FW",
    opts=pulumi.ResourceOptions(depends_on=[networking_resource_group]),
    route_table_name=str(network_resource_prefix) + "-to-FW",
    location=networking_resource_group.location,
    resource_group_name=networking_resource_group.name,
    disable_bgp_route_propagation=False,
    routes=build_routes(
        config_route_tables["VnetToFw"],
        network_resource_prefix,
        {},
        trust_nic_private_ip,
        untrust_nic_private_ip,
        cfg,
    ),
)
# Create NSG: custom rules from nsg_rules when non-empty; omit security_rules when [] / missing (Azure defaults only).
spoke_network_security_group = azure_classic.network.NetworkSecurityGroup(str(spoke_prefix) + "-NSG",
    opts=pulumi.ResourceOptions(depends_on=[networking_resource_group]),
    name=str(spoke_prefix) + "-NSG",
    location=networking_resource_group.location,
    resource_group_name=networking_resource_group.name,
    **({"security_rules": build_hub_nsg_rules(config_nsg_rules, cfg)} if config_nsg_rules else {}),
)

# Create a VNET with one Subnet
vnet1 = azure_native.network.VirtualNetwork(str(spoke_prefix) + "-VNET",
    virtual_network_name=str(spoke_prefix) + "-VNET",
    address_space=azure_native.network.AddressSpaceArgs(address_prefixes=[vnet1_cidr]),
    location=networking_resource_group.location,
    resource_group_name=networking_resource_group.name,
    subnets=[
        azure_native.network.SubnetArgs(
            name=str(spoke_prefix) + "-subnet1",
            address_prefix=vnet1_cidr)
    ])

# Associate Network Security Group
vnet1_network_security_group_association = azure_classic.network.SubnetNetworkSecurityGroupAssociation("DefaultNetworkSecurityGroupAssociation",
    subnet_id=vnet1.subnets[0].id,
    opts=pulumi.ResourceOptions(depends_on=[spoke_network_security_group, vnet1]),
    network_security_group_id=spoke_network_security_group.id)

# Associate Route Table
vnet1_route_table_association = azure_classic.network.SubnetRouteTableAssociation("VnetRouteTableAssociation",
    subnet_id=vnet1.subnets[0].id,
    opts=pulumi.ResourceOptions(depends_on=[vnet_to_fw_route_table,vnet1]),
    route_table_id=vnet_to_fw_route_table.id)

# VNET peerings from Pulumi.<stack>.yaml (list: name, remote_vnet_id, cidr). Create only when list has items.
vnet_peerings = []
if config_peerings:
    for i, p in enumerate(config_peerings):
        peering = azure_native.network.VirtualNetworkPeering(p["name"],
            virtual_network_peering_name=p["name"],
            allow_forwarded_traffic=True,
            allow_gateway_transit=False,
            allow_virtual_network_access=True,
            remote_virtual_network=azure_native.network.SubResourceArgs(id=p["remote_vnet_id"]),
            resource_group_name=networking_resource_group.name,
            use_remote_gateways=True,
            virtual_network_name=vnet1.name,
        )
        vnet_peerings.append(peering)

# Export VNET CIDR Block
pulumi.export(str(spoke_prefix) + "-VNET", vnet1_cidr)
for p in config_peerings:
    pulumi.export(f"{p['name']} Peering CIDR", p["cidr"])