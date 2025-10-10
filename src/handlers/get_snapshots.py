"""Lambda handler for retrieving snapshot IDs for resources to restore."""

import os
import sys
from typing import Dict, Any, List, Optional

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
    if snapshot_date:
        print(f"Filtering snapshots for date: {snapshot_date}")

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
                start_date=snapshot_date,
                end_date=snapshot_date,
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

            # Pass through instance type/class for mirroring source configuration
            if resource_type == "AWS_EC2" and resource.get("instanceType"):
                snapshot_data["instanceType"] = resource.get("instanceType")
            elif resource_type == "AWS_RDS" and resource.get("dbInstanceClass"):
                snapshot_data["dbInstanceClass"] = resource.get("dbInstanceClass")

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
