"""Lambda handler for retrieving snapshot IDs for resources to restore."""

import os
import sys
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

import boto3

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials

# Sentinel accepted in `snapshotDate` to mean "each resource's most recent
# snapshot, whenever it was taken". `null` means the same thing.
LATEST_SNAPSHOT = "latest"

# Cap on how many skipped resources are carried through the state machine.
# The full list always goes to CloudWatch; this only bounds the Step Functions
# payload, which has a hard 256 KB limit.
MAX_REPORTED_SKIPS = 100


def parse_snapshot_date(snapshot_date: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Turn the `snapshotDate` input into an inclusive (start, end) filter.

    Accepts `None` or "latest" for the newest snapshot per resource, or a
    YYYY-MM-DD date to pin the run to snapshots taken on that day.

    Returns:
        (start_date, end_date), both None when selecting the latest snapshot.
    """
    if snapshot_date is None:
        return None, None

    value = str(snapshot_date).strip()
    if not value or value.lower() == LATEST_SNAPSHOT:
        return None, None

    try:
        date_obj = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"Invalid snapshotDate '{snapshot_date}'. Use a YYYY-MM-DD date, "
            f"\"{LATEST_SNAPSHOT}\", or null to restore each resource's most recent snapshot."
        )

    # Add 1 day to end_date to make it inclusive (API treats end_date as exclusive)
    end_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    return value, end_date


def send_no_snapshots_notification(
    source_account_id: Optional[str],
    snapshot_date: Optional[str],
    resource_count: int,
    skipped: List[Dict[str, Any]]
) -> None:
    """
    Alert that the run found resources but no snapshots to restore from.

    Without this the workflow initiates zero jobs and reports success, which
    reads identically to a recovery that worked.
    """
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not sns_topic_arn:
        print("No SNS topic ARN configured, skipping notification")
        return

    if snapshot_date and str(snapshot_date).lower() != LATEST_SNAPSHOT:
        scope = f"on {snapshot_date}"
    else:
        scope = "at all"

    message_lines = [
        "Eon Bulk Recovery Status Report",
        "=" * 50,
        "",
        f"No restore jobs were started: {resource_count} backed-up resource(s) were found in the "
        f"source account, but none of them have a snapshot {scope}.",
        "",
        "Recovery Details:",
        "-" * 50,
        f"Source Account: {source_account_id or 'Unknown'}",
        f"Requested Snapshot Date: {snapshot_date if snapshot_date else 'latest'}",
        f"Resources In Scope: {resource_count}",
        "",
        "What to check:",
        "-" * 50,
        "- Confirm a backup ran on the requested date; the Eon console lists each resource's snapshots.",
        f"- Re-run with \"snapshotDate\": \"{LATEST_SNAPSHOT}\" to restore each resource's most recent snapshot.",
        "- Narrow or widen the run with the resourceTypes and resourceIds inputs.",
        "",
        "Resources Without a Snapshot:",
        "-" * 50,
    ]

    for entry in skipped[:MAX_REPORTED_SKIPS]:
        message_lines.append(
            f"- {entry.get('resourceName')} ({entry.get('resourceType')}) "
            f"in {entry.get('region') or 'unknown region'}: {entry.get('reason')}"
        )

    if len(skipped) > MAX_REPORTED_SKIPS:
        message_lines.append(f"... and {len(skipped) - MAX_REPORTED_SKIPS} more (see CloudWatch logs)")

    try:
        boto3.client("sns").publish(
            TopicArn=sns_topic_arn,
            Subject="Eon Bulk Recovery - NO SNAPSHOTS FOUND",
            Message="\n".join(message_lines)
        )
        print("Sent 'no snapshots found' notification")
    except Exception as e:
        print(f"ERROR: Failed to send 'no snapshots found' notification: {str(e)}")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Retrieve snapshot IDs for all resources to be restored.

    Input event:
        resources: List of resources from list_resources step
        snapshotDate: Date (YYYY-MM-DD) to pin snapshot selection to, or
            "latest"/null for each resource's most recent snapshot
        sourceAccountId: Source account, for the notification (optional)

    Returns:
        resourceSnapshots: List of resources with their selected snapshot IDs
        totalSnapshots: Total number of snapshots to restore
        resourcesWithoutSnapshots: Resources that yielded nothing to restore
        resourcesWithoutSnapshotsCount: Full count of the above (the list is capped)
    """
    resources = event["resources"]
    snapshot_date = event.get("snapshotDate")
    source_account_id = event.get("sourceAccountId")

    start_date, end_date = parse_snapshot_date(snapshot_date)

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
    if start_date:
        print(f"Filtering snapshots to date {start_date} (range {start_date} to {end_date})")
    else:
        print("Selecting each resource's most recent snapshot")

    resource_snapshots = []
    resources_without_snapshots = []

    def skip(resource: Dict[str, Any], reason: str) -> None:
        """Record a resource that will not be restored, and why."""
        print(f"WARNING: Skipping {resource.get('resourceName')} - {reason}")
        resources_without_snapshots.append({
            "resourceId": resource.get("id"),
            "resourceName": resource.get("resourceName"),
            "resourceType": resource.get("resourceType"),
            "region": resource.get("region"),
            "reason": reason
        })

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
                if start_date:
                    latest = resource.get("latestSnapshotTime")
                    detail = f", latest available is {latest}" if latest else ""
                    skip(resource, f"no snapshot taken on {start_date}{detail}")
                else:
                    skip(resource, "no snapshots exist for this resource")
                continue

            # Take the first snapshot (sorted by pointInTime DESC, so this is the latest)
            selected_snapshot = snapshots[0]

            # Extract resource-specific properties from the snapshot
            snapshot_resource = selected_snapshot.get("resource", {})
            snapshot_properties = snapshot_resource.get("properties", {})

            # Extract original tags from the snapshot resource
            original_tags = snapshot_resource.get("tags", {})

            snapshot_data = {
                "resourceId": resource_id,
                "resourceName": resource_name,
                "resourceType": resource_type,
                "providerResourceId": resource.get("providerResourceId"),
                "region": resource.get("region"),
                "vpc": resource.get("vpc"),
                "subnets": resource.get("subnets", []),
                "snapshotId": selected_snapshot["id"],
                "snapshotPointInTime": selected_snapshot["pointInTime"],
                "originalTags": original_tags
            }

            print(f"Resource {resource_name} region: {resource.get('region')}, tags: {len(original_tags)}")

            # For EC2 instances, extract configuration from snapshot (but NOT networking config)
            if resource_type == "AWS_EC2":
                aws_ec2 = snapshot_properties.get("awsEc2", {})
                if not aws_ec2:
                    skip(resource, "snapshot has no awsEc2 properties")
                    continue

                # Extract instance type, volumes, and IAM profile
                # Note: Networking config (subnet, security groups) will come from vpcConfigs
                snapshot_data["instanceType"] = aws_ec2.get("instanceType")
                snapshot_data["instanceProfileName"] = aws_ec2.get("instanceProfileName")
                snapshot_data["volumes"] = aws_ec2.get("volumes", [])

                # If no volumes, there's no data to restore - skip this resource
                if not snapshot_data["volumes"]:
                    skip(resource, "snapshot contains no volumes")
                    continue

                print(f"Extracted EC2 config: instance_type={snapshot_data['instanceType']}, "
                      f"volumes={len(snapshot_data.get('volumes', []))}, "
                      f"instance_profile={snapshot_data.get('instanceProfileName', 'None')}")

            # For RDS instances, carry through DB instance class and engine for class-availability validation
            elif resource_type == "AWS_RDS":
                snapshot_data["dbInstanceClass"] = resource.get("dbInstanceClass")
                snapshot_data["engine"] = resource.get("engine")

            # For DynamoDB tables, use table size from the resource listing (sourceStorage.sizeBytes)
            # Note: the snapshot API does NOT include sourceStorage — it's only on the resource API
            elif resource_type == "AWS_DYNAMO_DB":
                table_size_bytes = resource.get("tableSizeBytes", 0)

                snapshot_data["tableSizeBytes"] = table_size_bytes

                # Convert bytes to GB for easier reading
                table_size_gb = table_size_bytes / (1024 ** 3) if table_size_bytes > 0 else 0

                print(f"DynamoDB table size (from resource): {table_size_gb:.2f} GB ({table_size_bytes:,} bytes)")

                if table_size_bytes == 0:
                    print(f"WARNING: No size information available for {resource_name}, will use equal WCU distribution")

            resource_snapshots.append(snapshot_data)

            print(f"Selected snapshot {selected_snapshot['id']} from {selected_snapshot['pointInTime']}")

        except Exception as e:
            # Continue with other resources even if one fails, but record the miss
            # so it shows up in the report rather than vanishing.
            skip(resource, f"snapshot lookup failed: {str(e)}")
            continue

    print(f"Successfully retrieved {len(resource_snapshots)} snapshots")

    if resources_without_snapshots:
        print(f"{len(resources_without_snapshots)} resource(s) have nothing to restore:")
        for entry in resources_without_snapshots:
            print(f"  - {entry['resourceName']} ({entry['resourceType']}): {entry['reason']}")

    # Resources were found but none of them can be restored. Tell someone —
    # otherwise the workflow initiates no jobs and reports success.
    if resources and not resource_snapshots:
        send_no_snapshots_notification(
            source_account_id=source_account_id,
            snapshot_date=snapshot_date,
            resource_count=len(resources),
            skipped=resources_without_snapshots
        )

    return {
        "resourceSnapshots": resource_snapshots,
        "totalSnapshots": len(resource_snapshots),
        "snapshotDate": snapshot_date,
        "resourcesWithoutSnapshots": resources_without_snapshots[:MAX_REPORTED_SKIPS],
        "resourcesWithoutSnapshotsCount": len(resources_without_snapshots)
    }
