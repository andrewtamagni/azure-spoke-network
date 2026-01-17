# This script gets all the available VNETs that connect from on-prem to one of the Cloud Providers.
# You must be logged into the applicable Azure CLI with valid credentials before running this script!

# Developed on 12/2021 by Andrew Tamagni - UCAR STO
# Updated 10/22/2025 by Andrew Tamagni - UCAR STO: Added Pulumi stack support

# install the Python Azure CLI
# pip install azure-cli

import os
import re
import subprocess
import sys
import yaml
import pulumi
import argparse
import ipaddress
import subprocess

try:
    cfg = pulumi.Config()
    config_cloud_network_spaces = cfg.require_object("cloud_network_spaces")
    # Try to get cloud_network_env from config
    try:
        config_cloud_network_env = cfg.get("cloud_network_env")
    except Exception:
        config_cloud_network_env = None
except Exception:
    # Fallback when not running under `pulumi up`
    try:
        with open("Pulumi.yaml", "r", encoding="utf-8") as f:
            project = yaml.safe_load(f)["name"]

        stack = os.getenv("PULUMI_STACK")
        if not stack:
            try:
                out = subprocess.run(["pulumi", "stack"], check=True, capture_output=True, text=True).stdout
                stack = re.search(r"Current stack is ([^\s:]+):", out).group(1).split("/", 1)[-1]
            except (subprocess.CalledProcessError, FileNotFoundError):
                # If pulumi command fails, try to find stack files
                stack_files = [f for f in os.listdir(".") if f.startswith("Pulumi.") and f.endswith(".yaml")]
                if stack_files:
                    stack = stack_files[0].replace("Pulumi.", "").replace(".yaml", "")
                else:
                    raise Exception("No Pulumi stack found")

        with open(f"Pulumi.{stack}.yaml", "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}

        config_cloud_network_spaces = y["config"][f"{project}:cloud_network_spaces"]
        # Try to get cloud_network_env from config
        config_cloud_network_env = y["config"].get(f"{project}:cloud_network_env")
    except Exception as e:
        print(f"Error loading Pulumi configuration: {e}")
        print("Please ensure you are in a Pulumi project directory with valid configuration files.")
        sys.exit(1)

# Read in all variables from the Pulumi.<stack>.yaml file
azure_prod_address_space = config_cloud_network_spaces["azure_prod"]["vnet_cidr"]
azure_test_address_space = config_cloud_network_spaces["azure_test"]["vnet_cidr"]
#aws_prod_address_space = config_cloud_network_spaces.require_object('aws_prod')
#aws_test_address_space = config_cloud_network_spaces.require_object('aws_test')
#gcp_prod_address_space = config_cloud_network_spaces.require_object('gcp_prod')
#gcp_test_address_space = config_cloud_network_spaces.require_object('gcp_test')

# Create a parser to display help message and validate arguments
def parse_arguments():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('CloudProvider', nargs='?', default=None,
        help="""
        Enter Cloud Provider (optional - will try to get from cloud_network_env config first)

        Examples:

        python GetAvailableCloudSubnets.py /28  (uses cloud_network_env from config)
        python GetAvailableCloudSubnets.py azure /28
        python GetAvailableCloudSubnets.py gcp /28
        python GetAvailableCloudSubnets.py aws-test /28
        """,
        choices=["azure","azure-test","aws","aws-test","gcp","gcp-test"])
    parser.add_argument(dest='MaskBits',
        help="""
        Please enter the argument for Subnet Mask Bits or number of host IPs needed for the network.

        Examples for getting a /28 Subnet with 14 availible hosts:
    
        python GetAvailableCloudSubnets.py /28  (uses cloud_network_env from config)
        python GetAvailableCloudSubnets.py azure /28

        Availible Network CIDR sizes include:

        /24 with 254 Host IPs
        /25 with 126 Host IPs
        /26 with 62 Host IPs
        /27 with 30 Host IPs
        /28 with 14 Host IPs
        /29 with 6 Host IPs""",
        choices=["/24","/25","/26","/27","/28","/29"])
    args = parser.parse_args()
    return args

def GetAvailableSubnets(AddressSpace,ExistingVnets,MaskBits):
    global result
    AvailableVnets = [] # Empty array for availible VNETs
    ExistingVnets = ExistingVnets.split(",") # Split ExistingVnets argument
    PossibleNetworks = list(ipaddress.ip_network(AddressSpace).subnets(new_prefix=MaskBits)) # All possible VNETs for the MaskBits argument
    AddressSpaceIPs = list(ipaddress.ip_network(AddressSpace)) # All possible IPs in an AddressSpace    

    # Check if Array for existing VNETs is empty.  If it isn't, remove All Vnet possible IPs from AddressSpaceIPs
    if (ExistingVnets != ['']):
        for Vnet in ExistingVnets:
            ipVnet = ipaddress.ip_network(Vnet)
            for ip in ipVnet:
                if ipaddress.IPv4Address(ip) in AddressSpaceIPs:
                    AddressSpaceIPs.remove(ipaddress.IPv4Address(ip))

    # Iterate through all possible networks.  Select only networks that have all IPs availible 
    for PossibleNetwork in PossibleNetworks:
        hosts = list(ipaddress.ip_network(PossibleNetwork).hosts())
        check = all(item in AddressSpaceIPs for item in hosts)
        #print(PossibleNetwork)
        #print(check)
        if check == True:
            AvailableVnets.append(str(PossibleNetwork))

    # Return the first available VNET and generate a warning if there are no VNETs available for the requested size       
    if (AvailableVnets != []):
        # Return only the first availible VNET
        result = ipaddress.ip_network(AvailableVnets[0])
        return result        
    else:
        print('There are no more /%d' %MaskBits,"Networks Available")
        sys.exit(1)

# Get all the current networks being used in an Azure tenant by setting the AzureAddressSpaces variable.
def GetAzureOnPremVnets(AzureAddressSpace,MaskBits):
    # Load Azure Modules
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.resource import SubscriptionClient

    AzureOnPremVnets = [] # Empty array for existing Azure networks that connect on-prem
    subscription_client = SubscriptionClient(credential = DefaultAzureCredential(exclude_visual_studio_code_credential=True)) # Establish Subscription Client

    # Grab list of subscriptions and run nested FOR loops to grab
    # the network address prefix for each
    # Subscription / Resource Group / Resource / Microsoft.Network/virtualNetworks 
    for subscription in subscription_client.subscriptions.list():
        resource_client = ResourceManagementClient(DefaultAzureCredential(exclude_visual_studio_code_credential=True),subscription.subscription_id) # Establish Resource Client
        network_client = NetworkManagementClient(DefaultAzureCredential(exclude_visual_studio_code_credential=True),subscription.subscription_id) # Establish Network Client
        group_list = resource_client.resource_groups.list() # Grab the list of Resource Groups for each Subscription

        for group in group_list:
            # Grab a list of all resources in a Resource Group
            resource_list = resource_client.resources.list_by_resource_group(group.name)
        
            for resource in resource_list:
                # Filter out only vnet resources
                if (resource.type == "Microsoft.Network/virtualNetworks"):
                    # Establish Network Client
                    network = network_client.virtual_networks.get(group.name,resource.name)
                    
                    # Populate AzureOnPremVnets Array with all address prefixes                
                    for vnet in network.address_space.address_prefixes:
                        # Filter out networks that are not in OnPrem VNET 
                        if ipaddress.ip_network(vnet).subnet_of(ipaddress.ip_network(AzureAddressSpace)):
                            AzureOnPremVnets.append(vnet)
    #Join array to pass as one argument
    AzureOnPremVnets = ",".join(AzureOnPremVnets)

    # Run GetAvailableSubnets with the results
    GetAvailableSubnets(AzureAddressSpace,AzureOnPremVnets,MaskBits)

def GetAwsOnPremVnets(AwsAddressSpace,MaskBits):
    global result
    result = "Script Ready for AWS Module"
    return result

def GetGcpOnPremVnets(GcpAddressSpace,MaskBits):
    global result
    result = "Script Ready for GCP Module"
    return result

def main(CloudProvider,MaskBitSize):    
    # set MaskBits agrument
    if ((MaskBitSize == "/29")):MaskBits = 29
    if ((MaskBitSize == "/28")):MaskBits = 28
    if ((MaskBitSize == "/27")):MaskBits = 27
    if ((MaskBitSize == "/26")):MaskBits = 26
    if ((MaskBitSize == "/25")):MaskBits = 25
    if ((MaskBitSize == "/24")):MaskBits = 24

    # If CloudProvider is not provided, try to get it from config
    if CloudProvider is None:
        if config_cloud_network_env:
            CloudProvider = config_cloud_network_env
        else:
            print("Error: CloudProvider argument is required when cloud_network_env is not set in Pulumi config")
            print("Please provide CloudProvider as an argument or set cloud_network_env in your Pulumi config")
            sys.exit(1)

    ##### MAIN SCRIPT #####
    if (CloudProvider == "azure"):GetAzureOnPremVnets(azure_prod_address_space,MaskBits)
    if (CloudProvider == "azure-test"):GetAzureOnPremVnets(azure_test_address_space,MaskBits)
    #if (CloudProvider == "aws"):GetAwsOnPremVnets(aws_prod_address_space,MaskBits)
    #if (CloudProvider == "aws-test"):GetAwsOnPremVnets(aws_test_address_space,MaskBits)
    #if (CloudProvider == "gcp"):GetGcpOnPremVnets(gcp_prod_address_space,MaskBits)
    #if (CloudProvider == "gcp-test"):GetGcpOnPremVnets(gcp_test_address_space,MaskBits)

    return result

# Allow the script to run from command prompt or be imported into another script as a module
if __name__ == '__main__':
    args = parse_arguments()
    print(main(args.CloudProvider,args.MaskBits))