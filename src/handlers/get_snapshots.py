"""Lambda handler for retrieving snapshot IDs for resources to restore."""

import os
import sys
from typing import Dict, Any
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Retrieve snapshot IDs for all resources to be restored.

    Input event:
        resources: List of resources from list_resources step
        snapshotDate: Optional date (YYYY-MM-DD) to filter snapshots

    Returns:
        resourceSnapshots: List of resources with their selected snapshot IDs
        totalSnapshots: Total number of snapshots to restore
    """
    resources = event["resources"]
    snapshot_date = event.get("snapshotDate")

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    print(f"Retrieving snapshots for {len(resources)} resources")

    # Calculate date range for filtering
    start_date = None
    end_date = None
    if snapshot_date:
        print(f"Filtering snapshots for date: {snapshot_date}")
        start_date = snapshot_date
        # Add 1 day to end_date to make it inclusive (API treats end_date as exclusive)
        date_obj = datetime.strptime(snapshot_date, "%Y-%m-%d")
        end_date_obj = date_obj + timedelta(days=1)
        end_date = end_date_obj.strftime("%Y-%m-%d")
        print(f"Using date range: {start_date} to {end_date}")

    resource_snapshots = []

    for resource in resources:
        resource_id = resource["id"]
        resource_name = resource["resourceName"]
        resource_type = resource["resourceType"]

        print(f"Getting snapshots for resource: {resource_name} (ID: {resource_id})")

        try:
            # Get snapshots for this resource
            response = eon_client.list_snapshots(
                resource_id=resource_id,
                start_date=start_date,
                end_date=end_date,
                page_size=10  # We only need the latest snapshot
            )

            snapshots = response.get("snapshots", [])

            if not snapshots:
                print(f"WARNING: No snapshots found for resource {resource_name}")
                continue

            # Take the first snapshot (sorted by pointInTime DESC, so this is the latest)
            selected_snapshot = snapshots[0]

            snapshot_data = {
                "resourceId": resource_id,
                "resourceName": resource_name,
                "resourceType": resource_type,
                "providerResourceId": resource.get("providerResourceId"),
                "region": resource.get("region"),
                "vpc": resource.get("vpc"),
                "subnets": resource.get("subnets", []),
                "snapshotId": selected_snapshot["id"],
                "snapshotPointInTime": selected_snapshot["pointInTime"]
            }

            print(f"Resource {resource_name} region: {resource.get('region')}")

            # Extract resource-specific properties from the snapshot
            snapshot_resource = selected_snapshot.get("resource", {})
            snapshot_properties = snapshot_resource.get("properties", {})

            # For EC2 instances, extract configuration from snapshot (but NOT networking config)
            if resource_type == "AWS_EC2":
                aws_ec2 = snapshot_properties.get("awsEc2", {})
                if not aws_ec2:
                    print(f"WARNING: No awsEc2 properties found in snapshot for {resource_name}, skipping")
                    continue

                # Extract instance type, volumes, and IAM profile
                # Note: Networking config (subnet, security groups) will come from vpcConfigs
                snapshot_data["instanceType"] = aws_ec2.get("instanceType")
                snapshot_data["instanceProfileName"] = aws_ec2.get("instanceProfileName")
                snapshot_data["volumes"] = aws_ec2.get("volumes", [])

                # If no volumes, there's no data to restore - skip this resource
                if not snapshot_data["volumes"]:
                    print(f"WARNING: No volumes found in snapshot for {resource_name}, skipping")
                    continue

                print(f"Extracted EC2 config: instance_type={snapshot_data['instanceType']}, "
                      f"volumes={len(snapshot_data.get('volumes', []))}, "
                      f"instance_profile={snapshot_data.get('instanceProfileName', 'None')}")

            # For RDS instances, extract DB instance class
            elif resource_type == "AWS_RDS" and resource.get("dbInstanceClass"):
                snapshot_data["dbInstanceClass"] = resource.get("dbInstanceClass")

            # For DynamoDB tables, extract table size from sourceStorage
            elif resource_type == "AWS_DYNAMO_DB":
                source_storage = resource.get("sourceStorage", {})
                table_size_bytes = source_storage.get("sizeBytes", 0)

                snapshot_data["tableSizeBytes"] = table_size_bytes

                # Convert bytes to GB for easier reading
                table_size_gb = table_size_bytes / (1024 ** 3) if table_size_bytes > 0 else 0

                print(f"Extracted DynamoDB table size: {table_size_gb:.2f} GB ({table_size_bytes:,} bytes)")

            resource_snapshots.append(snapshot_data)

            print(f"Selected snapshot {selected_snapshot['id']} from {selected_snapshot['pointInTime']}")

        except Exception as e:
            print(f"ERROR: Failed to get snapshots for resource {resource_name}: {str(e)}")
            # Continue with other resources even if one fails
            continue

    print(f"Successfully retrieved {len(resource_snapshots)} snapshots")

    return {
        "resourceSnapshots": resource_snapshots,
        "totalSnapshots": len(resource_snapshots),
        "snapshotDate": snapshot_date
    }
