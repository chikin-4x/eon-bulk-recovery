"""Lambda handler for listing backed up resources from the source account."""

import os
import sys
from typing import Dict, Any, List

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    List all protected resources from the source account.

    Input event:
        sourceAccountId: AWS account ID to list resources from

    Returns:
        resources: List of protected resources with their details
        totalCount: Total number of protected resources found
    """
    source_account_id = event["sourceAccountId"]

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # List all protected resources with pagination
    print(f"Listing protected resources from source account: {source_account_id}")

    all_resources = []
    page_token = None

    while True:
        response = eon_client.list_resources(
            source_account_id=source_account_id,
            page_token=page_token,
            page_size=100
        )

        resources = response.get("resources", [])
        all_resources.extend(resources)

        page_token = response.get("nextToken")
        if not page_token:
            break

        print(f"Retrieved {len(all_resources)} resources so far, fetching next page...")

    print(f"Total protected resources found: {len(all_resources)}")

    # Filter to only include supported resource types
    supported_types = ["AWS_EC2", "AWS_RDS", "AWS_S3", "AWS_DYNAMO_DB"]
    filtered_resources = [
        r for r in all_resources
        if r.get("resourceType") in supported_types
    ]

    print(f"Filtered to {len(filtered_resources)} resources of supported types: {supported_types}")

    # Extract relevant information for each resource
    resource_list = []
    for resource in filtered_resources:
        # Extract resource properties first to get region from correct location
        resource_properties = resource.get("resourceProperties", {})

        resource_data = {
            "id": resource.get("id"),
            "resourceName": resource.get("resourceName"),
            "resourceType": resource.get("resourceType"),
            "providerResourceId": resource.get("providerResourceId"),
            "region": resource_properties.get("region") or resource.get("region"),
            "vpc": resource.get("vpc"),
            "subnets": resource.get("subnets", []),
            "latestSnapshotTime": resource.get("latestSnapshotTime")
        }

        # Extract instance type/class from resourceProperties for mirroring source configuration
        if resource.get("resourceType") == "AWS_EC2":
            resource_data["instanceType"] = resource_properties.get("instanceType")
        elif resource.get("resourceType") == "AWS_RDS":
            resource_data["dbInstanceClass"] = resource_properties.get("dbInstanceClass")
        elif resource.get("resourceType") == "AWS_DYNAMO_DB":
            source_storage = resource.get("sourceStorage", {})
            resource_data["tableSizeBytes"] = source_storage.get("sizeBytes", 0)

        # Debug: Log region extraction
        print(f"Resource {resource_data['resourceName']} ({resource_data['resourceType']}) - region: {resource_data['region']}")

        resource_list.append(resource_data)

    return {
        "resources": resource_list,
        "totalCount": len(resource_list),
        "sourceAccountId": source_account_id
    }
