"""An Azure RM Python Pulumi program"""
# Developed on 12/2021 by Andrew Tamagni - STO
# Updated 10/22/2025 by Andrew Tamagni - STO: Converted Windows VM to Ubuntu VM.  Added Peering to Hub VNET.

import pulumi
from pulumi_azure_native import storage
from pulumi_azure_native import resources
import pulumi_azure_native as azure_native
import pulumi_azure_native.network as azure_native_networking
import pulumi_azure as azure_classic
from pulumi_azure_native.network import network_security_group
from pulumi_azure_native.network.network_security_group import NetworkSecurityGroup

pulumi.log.info("Deploying Resources")

cfg = pulumi.Config()
cfg_az = pulumi.Config("azure-native")
rg_prefix = cfg.require("rg_prefix")
vnet1 = cfg.require_object("vnet1")
vnet1_cidr = vnet1["cidr"]
vnet1_prefix = vnet1["prefix"]
vm1 = cfg.require_object("vm1")
vm1_vm_name = vm1["name"]
vm1_admin_username = vm1["admin_username"]
vm1_admin_password = vm1["admin_pw"]
on_prem_source_ip_range = cfg.require("on_prem_source_ip_range")
trust_network_interface = cfg.require("trust_network_interface")

# Peerings
config_peerings = cfg.require_object('peerings')
hub_vnet_id = config_peerings['hub_vnet_id']
hub_cidr = config_peerings['hub_cidr']

# Create an Azure Resource Group
resource_group = resources.ResourceGroup(str(rg_prefix) + "-Networking",
    resource_group_name=str(rg_prefix) + "-Networking")

VM1_resource_group = resources.ResourceGroup(str(rg_prefix) + "-" + str(vm1_vm_name) + "-VM",
    resource_group_name=str(rg_prefix) + "-" + str(vm1_vm_name) + "-VM",
)

# Create Route Table
VnetToFw_route_table = azure_native.network.RouteTable(str(rg_prefix) + "-to-FW",
    route_table_name=str(rg_prefix) + "-to-FW",
    location=resource_group.location,
    resource_group_name=resource_group.name,
    disable_bgp_route_propagation=False,
    routes=[
        azure_native.network.RouteArgs(name=str(vnet1_prefix) + "-to-FW-Route1",
        address_prefix="0.0.0.0/0",
        next_hop_type="VirtualAppliance",
        next_hop_ip_address=trust_network_interface),

        azure_native.network.RouteArgs(name=str(vnet1_prefix) + "-to-FW-Route2",
        address_prefix=on_prem_source_ip_range,
        next_hop_type="VirtualNetworkGateway"),

        azure_native.network.RouteArgs(name=str(vnet1_prefix) + "-to-FW-Route3",
        address_prefix="xx.xx.xx.xx/16",
        next_hop_type="VirtualNetworkGateway"),

        azure_native.network.RouteArgs(name=str(vnet1_prefix) + "-to-FW-Route4",
        address_prefix="xx.xx.xx.xx/12",
        next_hop_type="VirtualNetworkGateway"),

        azure_native.network.RouteArgs(name=str(vnet1_prefix) + "-to-FW-Route5",
        address_prefix=hub_cidr,
        next_hop_type="VirtualAppliance",
        next_hop_ip_address=trust_network_interface),
        
    ])
# Create Default Network Security Group
vnet_network_security_group = azure_native.network.NetworkSecurityGroup(str(vnet1_prefix) + "-nsg",
    network_security_group_name=str(vnet1_prefix) + "-nsg",
    location=resource_group.location,
    resource_group_name=resource_group.name,
    security_rules=[azure_classic.network.NetworkSecurityGroupSecurityRuleArgs(
        name="Allow-Outside-From-IP",
        description="Rule",
        protocol="*",
        source_port_range="*",
        destination_port_range="*",
        source_address_prefix=on_prem_source_ip_range,
        destination_address_prefix="*",
        access="Allow",
        priority=100,
        direction="Inbound"),
        
        azure_classic.network.NetworkSecurityGroupSecurityRuleArgs(
        name="Allow-Intra",
        description="Allow intra network traffic",
        protocol="*",
        source_port_range="*",
        destination_port_range="*",
        source_address_prefix=vnet1_cidr,
        destination_address_prefix="*",
        access="Allow",
        priority=101,
        direction="Inbound"),    

        azure_classic.network.NetworkSecurityGroupSecurityRuleArgs(
        name="Default-Deny",
        description="Deny if we don't match Allow rule",
        protocol="*",
        source_port_range="*",
        destination_port_range="*",
        source_address_prefix="*",
        destination_address_prefix="*",
        access="Deny",
        priority=200,
        direction="Inbound"), 
    ],
)

# Create a VNET with one Subnet
vnet1 = azure_native.network.VirtualNetwork(str(vnet1_prefix) + "-VNET",
    virtual_network_name=str(vnet1_prefix) + "-VNET",
    address_space=azure_native.network.AddressSpaceArgs(address_prefixes=[vnet1_cidr]),
    location=resource_group.location,
    resource_group_name=resource_group.name,
    subnets=[
        azure_native.network.SubnetArgs(
            name=str(vnet1_prefix) + "-subnet1",
            address_prefix=vnet1_cidr)
    ])

# Associate Network Security Group
subnet_network_security_group_association = azure_classic.network.SubnetNetworkSecurityGroupAssociation("DefaultNetworkSecurityGroupAssociation",
    subnet_id=vnet1.subnets[0].id,
    network_security_group_id=vnet_network_security_group.id)

# Associate Route Table
subnet_route_table_association = azure_classic.network.SubnetRouteTableAssociation("VnetRouteTableAssociation",
    subnet_id=vnet1.subnets[0].id,
    route_table_id=VnetToFw_route_table.id)

# Create VNET-to-HUB Peering
vnet_virtual_network_peering = azure_native.network.VirtualNetworkPeering("DEV-WEST1-to-HUB",
    allow_forwarded_traffic=True,
    allow_gateway_transit=False,
    allow_virtual_network_access=True,
    remote_virtual_network=azure_native.network.SubResourceArgs(
        id=hub_vnet_id,
    ),
    resource_group_name=resource_group.name,
    use_remote_gateways=True,
    virtual_network_name=vnet1.name,
    virtual_network_peering_name=str(vnet1_prefix) + "-VNET-to-HUB")

# Keeping around for testing
VM1_public_ip = azure_classic.network.PublicIp(str(vm1_vm_name) + "-Public-IP",
    opts=pulumi.ResourceOptions(depends_on=[VM1_resource_group]),
    name=str(vm1_vm_name) + "-Public-IP",
    resource_group_name=VM1_resource_group.name,
    location=VM1_resource_group.location,
    sku="Standard",
    sku_tier="Regional",
    allocation_method="Static",
    ip_version="IPv4",
    idle_timeout_in_minutes = 4,
    domain_name_label=str(vm1_vm_name),
)

# VM 1 Network interface
VM1_network_interface = azure_classic.network.NetworkInterface(str(vm1_vm_name) + "-eth0",
    #opts=pulumi.ResourceOptions(depends_on=[vnet1]),
    opts=pulumi.ResourceOptions(depends_on=[vnet1,VM1_public_ip]),
    name=str(vm1_vm_name) + "-eth0",
    location=VM1_resource_group.location,
    resource_group_name=VM1_resource_group.name,
    ip_configurations=[azure_classic.network.NetworkInterfaceIpConfigurationArgs(
        name="ipconfig1-" + str(vm1_vm_name),
        primary=True,
        subnet_id=vnet1.subnets[0].id,
        public_ip_address_id=VM1_public_ip.id, # For testing   
        private_ip_address_allocation="Dynamic",
        private_ip_address_version="IPv4")],
)

# VM 1 Virtual Machine
VM1_virtual_machine = azure_classic.compute.VirtualMachine(
    str(vm1_vm_name),
    opts=pulumi.ResourceOptions(depends_on=[VM1_network_interface]),
    name=str(vm1_vm_name),
    location=VM1_resource_group.location,
    resource_group_name=VM1_resource_group.name,
    network_interface_ids=[VM1_network_interface.id],
    primary_network_interface_id=VM1_network_interface.id,
    vm_size="Standard_A2_v2",

    storage_image_reference=azure_classic.compute.VirtualMachineStorageImageReferenceArgs(
        publisher="canonical",
        offer="ubuntu-24_04-lts",
        sku="minimal-gen1",
        version="latest",
    ),

    storage_os_disk=azure_classic.compute.VirtualMachineStorageOsDiskArgs(
        name=str(vm1_vm_name) + "_OsDisk_1",
        caching="ReadWrite",
        create_option="FromImage",
        managed_disk_type="Standard_LRS",
        os_type="Linux",
    ),

    os_profile=azure_classic.compute.VirtualMachineOsProfileArgs(
        computer_name=str(vm1_vm_name),
        admin_username=str(vm1_admin_username),
        admin_password=str(vm1_admin_password),
    ),

    os_profile_linux_config=azure_classic.compute.VirtualMachineOsProfileLinuxConfigArgs(
        disable_password_authentication=False
    ),
)

# Export VNET CIDR Block
pulumi.export(str(vnet1_prefix) + "-VNET", vnet1_cidr)
pulumi.export("VM 1 Hostname", vm1_vm_name)
pulumi.export(str(vm1_vm_name) + " Private IP", VM1_network_interface.private_ip_address)
pulumi.export(str(vm1_vm_name) + "-Public-IP FQDN", VM1_public_ip.fqdn)
pulumi.export(str(vm1_vm_name) + "-Public-IP", VM1_public_ip.ip_address.apply(lambda v: v or "pending…"))
pulumi.export("DEV-WEST1-to-HUB Peering CIDR", hub_cidr)