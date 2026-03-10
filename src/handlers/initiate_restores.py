"""Lambda handler for initiating restore jobs for all snapshots."""

import os
import sys
import hashlib
import re
import time
import random
from typing import Dict, Any, List, Optional
import boto3
from botocore.exceptions import ClientError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials, get_cross_account_credentials


def calculate_dynamodb_wcu_allocation_by_region(
    dynamodb_tables_by_region: Dict[str, List[Dict[str, Any]]],
    regional_wcu_capacity: int = 40000,
    utilization_percentage: float = 0.95,
    default_wcu_for_zero_size: int = 50
) -> Dict[str, int]:
    """
    Calculate WCU allocation for DynamoDB tables per-region based on their sizes.

    Args:
        dynamodb_tables_by_region: Dictionary mapping region to list of tables
        regional_wcu_capacity: WCU capacity per region (default 40000)
        utilization_percentage: Percentage of capacity to use (default 95%)
        default_wcu_for_zero_size: WCU for tables with 0 size (default 50)

    Returns:
        Dictionary mapping resource_id to allocated WCU
    """
    wcu_allocation = {}

    print(f"\nDynamoDB WCU Allocation (per-region):")
    print(f"  Regional capacity: {regional_wcu_capacity:,} WCU per region")
    print(f"  Utilization: {utilization_percentage*100}%")
    print(f"  Regions with DynamoDB tables: {len(dynamodb_tables_by_region)}")

    for region, all_tables in dynamodb_tables_by_region.items():
        if not all_tables:
            continue

        # Separate tables by size
        dynamodb_tables = []
        zero_size_tables = []

        for table in all_tables:
            if table["sizeBytes"] == 0:
                zero_size_tables.append(table)
            else:
                dynamodb_tables.append(table)

        # Calculate available WCUs for this region (95% of regional capacity)
        available_wcu = int(regional_wcu_capacity * utilization_percentage)

        print(f"\n  Region {region}:")
        print(f"    Available WCU: {available_wcu:,}")
        print(f"    Tables with size data: {len(dynamodb_tables)}")
        print(f"    Tables with zero size: {len(zero_size_tables)}")

        # Allocate sized tables first so they get proportional shares of capacity
        allocated_total = 0

        if dynamodb_tables:
            total_size = sum(table["sizeBytes"] for table in dynamodb_tables)

            print(f"    Total data size (sized tables): {total_size / (1024**3):.2f} GB")

            for table in dynamodb_tables:
                proportion = table["sizeBytes"] / total_size
                proportional_wcu = int(available_wcu * proportion)
                allocated_wcu = max(proportional_wcu, 1)

                wcu_allocation[table["resourceId"]] = allocated_wcu
                allocated_total += allocated_wcu

                table_size_gb = table["sizeBytes"] / (1024**3)
                print(f"    {table['resourceName']}: {table_size_gb:.2f} GB ({proportion*100:.1f}%) -> {allocated_wcu:,} WCU")

        # Give zero-size tables the default or whatever remains, whichever is smaller
        remaining_wcu = available_wcu - allocated_total

        for table in zero_size_tables:
            allocated_wcu = min(default_wcu_for_zero_size, max(remaining_wcu, 1))
            wcu_allocation[table["resourceId"]] = allocated_wcu
            remaining_wcu -= allocated_wcu
            allocated_total += allocated_wcu
            print(f"    {table['resourceName']}: 0 GB (no size data) -> {allocated_wcu:,} WCU")

        print(f"    Total allocated in {region}: {allocated_total:,} WCU ({allocated_total/regional_wcu_capacity*100:.1f}% of regional capacity)")

    return wcu_allocation


def discover_dynamodb_tables_from_stacks(
    stack_names: List[str],
    restore_account_credentials: Optional[Dict[str, str]] = None,
    regions: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Discover DynamoDB tables from CloudFormation stack resources.

    Queries CloudFormation stacks to find all AWS::DynamoDB::Table resources
    created by the stack. Works with any CloudFormation stack (CDK, SAM, plain CFN, etc.).

    Args:
        stack_names: List of CloudFormation stack names to scan
        restore_account_credentials: Cross-account credentials for restore account
        regions: List of regions to check for stacks (defaults to us-east-1)

    Returns:
        Dictionary mapping table names to their configuration:
        {
            "MyTable": {"tableName": "MyTable", "region": "us-east-1", "stackName": "MyStack"},
            ...
        }
    """
    if not stack_names:
        return {}

    if not regions:
        regions = ["us-east-1"]

    discovered_tables = {}

    for region in regions:
        # Create CloudFormation client
        if restore_account_credentials:
            cfn_client = boto3.client(
                "cloudformation",
                region_name=region,
                aws_access_key_id=restore_account_credentials["AccessKeyId"],
                aws_secret_access_key=restore_account_credentials["SecretAccessKey"],
                aws_session_token=restore_account_credentials["SessionToken"]
            )
        else:
            cfn_client = boto3.client("cloudformation", region_name=region)

        for stack_name in stack_names:
            try:
                # List all resources in the stack
                paginator = cfn_client.get_paginator("list_stack_resources")
                tables_found_in_stack = 0

                for page in paginator.paginate(StackName=stack_name):
                    for resource in page.get("StackResourceSummaries", []):
                        resource_type = resource.get("ResourceType", "")
                        resource_status = resource.get("ResourceStatus", "")

                        # Look for DynamoDB tables that are successfully created
                        if resource_type == "AWS::DynamoDB::Table" and resource_status in [
                            "CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"
                        ]:
                            table_name = resource.get("PhysicalResourceId")
                            logical_id = resource.get("LogicalResourceId", "")

                            if table_name:
                                discovered_tables[table_name] = {
                                    "tableName": table_name,
                                    "region": region,
                                    "stackName": stack_name,
                                    "logicalId": logical_id
                                }
                                tables_found_in_stack += 1
                                print(f"Discovered DynamoDB table from stack: {table_name} in {region} (stack: {stack_name}, logical ID: {logical_id})")

                if tables_found_in_stack > 0:
                    print(f"Found {tables_found_in_stack} DynamoDB table(s) in stack '{stack_name}' ({region})")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "ValidationError" and "does not exist" in str(e):
                    print(f"CloudFormation stack '{stack_name}' not found in region {region}")
                else:
                    print(f"Error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")
            except Exception as e:
                print(f"Unexpected error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")

    print(f"\nDiscovered {len(discovered_tables)} DynamoDB table(s) from stack(s) for in-place restore")
    return discovered_tables


def discover_s3_buckets_from_stacks(
    stack_names: List[str],
    restore_account_credentials: Optional[Dict[str, str]] = None,
    regions: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Discover S3 buckets from CloudFormation stack resources and index them by eon_functional_id tag.

    Queries CloudFormation stacks to find all AWS::S3::Bucket resources,
    then fetches the eon_functional_id tag from each bucket to build a lookup dict.

    Args:
        stack_names: List of CloudFormation stack names to scan
        restore_account_credentials: Cross-account credentials for restore account
        regions: List of regions to check for stacks (defaults to us-east-1)

    Returns:
        Dictionary mapping eon_functional_id tag value to bucket configuration:
        {
            "my-functional-id": {
                "bucketName": "actual-bucket-name",
                "region": "us-east-1",
                "stackName": "MyStack",
                "logicalId": "MyBucketResource",
                "eonFunctionalId": "my-functional-id"
            },
            ...
        }
    """
    if not stack_names:
        return {}

    if not regions:
        regions = ["us-east-1"]

    # Phase 1: Discover S3 bucket names from CloudFormation stacks
    discovered_buckets = []

    for region in regions:
        if restore_account_credentials:
            cfn_client = boto3.client(
                "cloudformation",
                region_name=region,
                aws_access_key_id=restore_account_credentials["AccessKeyId"],
                aws_secret_access_key=restore_account_credentials["SecretAccessKey"],
                aws_session_token=restore_account_credentials["SessionToken"]
            )
        else:
            cfn_client = boto3.client("cloudformation", region_name=region)

        for stack_name in stack_names:
            try:
                paginator = cfn_client.get_paginator("list_stack_resources")
                buckets_found_in_stack = 0

                for page in paginator.paginate(StackName=stack_name):
                    for resource in page.get("StackResourceSummaries", []):
                        resource_type = resource.get("ResourceType", "")
                        resource_status = resource.get("ResourceStatus", "")

                        if resource_type == "AWS::S3::Bucket" and resource_status in [
                            "CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"
                        ]:
                            bucket_name = resource.get("PhysicalResourceId")
                            logical_id = resource.get("LogicalResourceId", "")

                            if bucket_name:
                                discovered_buckets.append({
                                    "bucketName": bucket_name,
                                    "region": region,
                                    "stackName": stack_name,
                                    "logicalId": logical_id
                                })
                                buckets_found_in_stack += 1
                                print(f"Discovered S3 bucket from stack: {bucket_name} in {region} (stack: {stack_name}, logical ID: {logical_id})")

                if buckets_found_in_stack > 0:
                    print(f"Found {buckets_found_in_stack} S3 bucket(s) in stack '{stack_name}' ({region})")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "ValidationError" and "does not exist" in str(e):
                    print(f"CloudFormation stack '{stack_name}' not found in region {region}")
                else:
                    print(f"Error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")
            except Exception as e:
                print(f"Unexpected error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")

    # Phase 2: Fetch eon_functional_id tags from discovered buckets
    s3_buckets_by_functional_id = {}

    for bucket_info in discovered_buckets:
        if restore_account_credentials:
            s3_client = boto3.client(
                "s3",
                region_name=bucket_info["region"],
                aws_access_key_id=restore_account_credentials["AccessKeyId"],
                aws_secret_access_key=restore_account_credentials["SecretAccessKey"],
                aws_session_token=restore_account_credentials["SessionToken"]
            )
        else:
            s3_client = boto3.client("s3", region_name=bucket_info["region"])

        try:
            tagging_response = s3_client.get_bucket_tagging(Bucket=bucket_info["bucketName"])
            tag_set = tagging_response.get("TagSet", [])
            functional_id = None
            for tag in tag_set:
                if tag["Key"] == "eon_functional_id":
                    functional_id = tag["Value"]
                    break

            if functional_id:
                if functional_id in s3_buckets_by_functional_id:
                    print(f"WARNING: Duplicate eon_functional_id '{functional_id}' found on bucket '{bucket_info['bucketName']}', overwriting previous match")
                bucket_info["eonFunctionalId"] = functional_id
                s3_buckets_by_functional_id[functional_id] = bucket_info
                print(f"Discovered S3 bucket '{bucket_info['bucketName']}' with eon_functional_id='{functional_id}' (stack: {bucket_info['stackName']})")
            else:
                print(f"S3 bucket '{bucket_info['bucketName']}' has no eon_functional_id tag, skipping for in-place matching")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchTagSet":
                print(f"S3 bucket '{bucket_info['bucketName']}' has no tags, skipping for in-place matching")
            else:
                print(f"Error fetching tags for S3 bucket '{bucket_info['bucketName']}': {str(e)}")
        except Exception as e:
            print(f"Unexpected error fetching tags for S3 bucket '{bucket_info['bucketName']}': {str(e)}")

    print(f"\nDiscovered {len(s3_buckets_by_functional_id)} S3 bucket(s) with eon_functional_id from stack(s) for in-place restore")
    return s3_buckets_by_functional_id


def create_s3_bucket(bucket_name: str, region: str, kms_key_id: str, restore_account_id: str, snapshot_id: str, snapshot_point_in_time: str, original_tags: Dict[str, str] = None, restore_account_credentials: Dict[str, str] = None) -> None:
    """Create an S3 bucket in the restore account."""
    if original_tags is None:
        original_tags = {}

    # Use provided credentials or fall back to Lambda execution role
    if restore_account_credentials:
        s3_client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=restore_account_credentials["AccessKeyId"],
            aws_secret_access_key=restore_account_credentials["SecretAccessKey"],
            aws_session_token=restore_account_credentials["SessionToken"]
        )
    else:
        s3_client = boto3.client("s3", region_name=region)

    try:
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )

        # Enable default encryption
        s3_client.put_bucket_encryption(
            Bucket=bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": kms_key_id
                        },
                        "BucketKeyEnabled": True
                    }
                ]
            }
        )

        # Filter out AWS system tags (aws:, elasticbeanstalk:) as they cannot be manually set
        filtered_original_tags = {
            k: v for k, v in original_tags.items()
            if not k.lower().startswith(("aws:", "elasticbeanstalk:"))
        }

        # Merge filtered original tags with restore tags (restore tags take precedence)
        s3_tags = {
            **filtered_original_tags,
            "ManagedBy": "EonBulkRecovery",
            "Purpose": "RestoreDestination",
            "eon:restore": "true",
            "eon:snapshot_id": snapshot_id,
            "eon:snapshot_time": snapshot_point_in_time
        }

        # Convert tags dict to AWS TagSet format
        tag_set = [{"Key": k, "Value": v} for k, v in s3_tags.items()]

        # Add tags
        s3_client.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={"TagSet": tag_set}
        )

        print(f"Created S3 bucket: {bucket_name}")

    except ClientError as e:
        if e.response["Error"]["Code"] == "BucketAlreadyOwnedByYou":
            print(f"S3 bucket {bucket_name} already exists")
        else:
            raise


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Initiate restore jobs for all resource snapshots.

    Input event:
        resourceSnapshots: List of resources with snapshot IDs
        eonRestoreAccountId: Eon-assigned restore account ID
        restoreAccountId: AWS account ID of restore account
        restoreRegion: Primary region for restores
        kmsKeyArnsByRegion: Dictionary mapping region to KMS key ARN for encryption
        rdsSubnetGroupsByRegion: Dictionary mapping region to RDS subnet group name
        dynamodbRegionalWcuLimit: Regional WCU limit for DynamoDB (default 40000)
        vpcConfigs: VPC configurations for the restore
        crossAccountRoleArn: ARN of cross-account role (optional)
        excludeEC2TagKeys: List of tag keys to exclude from EC2 instance tags (optional)
        recoveryStackNames: List of CloudFormation stack names to check for pre-created DynamoDB tables (optional)
        recoveryStacksOnly: If true, only restore resources matching a stack table/bucket (optional, default false)

    Returns:
        restoreJobs: List of initiated restore jobs with job IDs
        totalJobs: Total number of jobs initiated
    """
    resource_snapshots = event["resourceSnapshots"]
    eon_restore_account_id = event["eonRestoreAccountId"]
    restore_account_id = event["restoreAccountId"]
    restore_region = event.get("restoreRegion", "us-east-1")
    kms_key_arns_by_region = event.get("kmsKeyArnsByRegion", {})
    rds_subnet_groups_by_region = event.get("rdsSubnetGroupsByRegion", {})
    dynamodb_regional_wcu_limit = event.get("dynamodbRegionalWcuLimit") or 40000
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")
    management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip() or None
    exclude_ec2_tag_keys = event.get("excludeEC2TagKeys", [])
    enable_cdk_recovery_stacks = event.get("recoveryStackNames", [])
    recovery_stacks_only = event.get("recoveryStacksOnly", False)
    resource_name_prefix = event.get("resourceNamePrefix")  # None means use original name

    # Helper function to generate restored resource name
    def get_restored_name(original_name: str) -> str:
        """Generate restored resource name based on prefix setting."""
        if resource_name_prefix:
            return f"{resource_name_prefix}{original_name}"
        return original_name  # Use original name for full account recovery

    def sanitize_s3_bucket_name(base_name: str, hash_suffix: str) -> str:
        """
        Build a globally-unique S3 bucket name from a base name and hash suffix.

        S3 bucket name constraints:
        - 3-63 lowercase alphanumeric characters, hyphens, or dots
        - Must begin and end with a letter or number
        - No consecutive periods (we also avoid consecutive hyphens for clarity)

        The hash_suffix is always preserved so the name stays globally unique.
        When the combined name exceeds 63 chars, the base is truncated to make
        room for ``-<hash_suffix>``.
        """
        max_len = 63
        suffix_with_sep = f"-{hash_suffix}"  # e.g. "-a1b2c3d4" (9 chars)

        combined = f"{base_name}{suffix_with_sep}".lower()

        if len(combined) <= max_len:
            name = combined
        else:
            # Truncate the base to leave room for the suffix
            allowed_base = max_len - len(suffix_with_sep)
            truncated = base_name[:allowed_base].rstrip("-")
            name = f"{truncated}{suffix_with_sep}".lower()

        # Collapse consecutive hyphens and strip leading/trailing hyphens
        name = re.sub(r"-{2,}", "-", name)
        name = name.strip("-")
        return name

    def sanitize_rds_identifier(name: str) -> str:
        """
        Sanitize a name to comply with RDS DB instance/cluster identifier constraints:
        - 1-63 lowercase alphanumeric characters or hyphens
        - Must begin with a letter
        - Can't end with a hyphen
        - Can't contain two consecutive hyphens

        When truncation is needed, appends a short hash of the original name
        to prevent collisions between names that share the same prefix.
        """
        original = name
        # Lowercase and replace invalid characters with hyphens
        name = name.lower()
        name = re.sub(r"[^a-z0-9-]", "-", name)
        # Collapse consecutive hyphens
        name = re.sub(r"-{2,}", "-", name)
        # Strip leading/trailing hyphens
        name = name.strip("-")
        # Ensure it starts with a letter
        if name and not name[0].isalpha():
            name = "r" + name
        # If empty after sanitization, generate from hash
        if not name:
            name = "r" + hashlib.sha256(original.encode()).hexdigest()[:8]
        # Truncate to 63 chars, using a hash suffix to avoid collisions
        max_len = 63
        if len(name) > max_len:
            suffix = hashlib.sha256(original.encode()).hexdigest()[:6]
            # Leave room for hyphen + 6-char hash suffix
            truncated = name[:max_len - 7].rstrip("-")
            name = f"{truncated}-{suffix}"
        # Final safety: strip any trailing hyphen (shouldn't happen but just in case)
        name = name.rstrip("-")
        return name

    print(f"KMS keys available in regions: {list(kms_key_arns_by_region.keys())}")
    print(f"RDS subnet groups available in regions: {list(rds_subnet_groups_by_region.keys())}")
    print(f"DynamoDB regional WCU limit: {dynamodb_regional_wcu_limit:,}")
    print(f"Resource name prefix: {resource_name_prefix if resource_name_prefix else '(none - using original names)'}")
    if exclude_ec2_tag_keys:
        print(f"EC2 tag keys to exclude: {exclude_ec2_tag_keys}")
    if enable_cdk_recovery_stacks:
        print(f"Recovery stacks to check: {enable_cdk_recovery_stacks}")
    if recovery_stacks_only:
        print(f"Recovery stacks ONLY mode: will skip resources without a matching stack table/bucket")

    # Get credentials for restore account once (reused throughout)
    restore_account_credentials = None
    try:
        restore_account_credentials = get_cross_account_credentials(
            restore_account_id=restore_account_id,
            cross_account_role_arn=cross_account_role_arn,
            management_account_id=management_account_id
        )
        print(f"Successfully obtained cross-account credentials for restore account {restore_account_id}")
    except Exception as e:
        print(f"WARNING: Could not obtain cross-account credentials: {str(e)}")
        print("Will attempt operations with Lambda execution role")

    # Discover DynamoDB table from stacks for in-place restore
    recovery_stack_tables = {}
    if enable_cdk_recovery_stacks:
        # Get regions from VPC configs to check for CloudFormation stacks
        vpc_regions = [config.get("region") for config in vpc_configs if config.get("region")]
        if not vpc_regions:
            vpc_regions = ["us-east-1"]

        recovery_stack_tables = discover_dynamodb_tables_from_stacks(
            stack_names=enable_cdk_recovery_stacks,
            restore_account_credentials=restore_account_credentials,
            regions=vpc_regions
        )

    # Discover S3 buckets from stacks for in-place restore (matched by eon_functional_id tag)
    recovery_stack_s3_buckets = {}
    if enable_cdk_recovery_stacks:
        recovery_stack_s3_buckets = discover_s3_buckets_from_stacks(
            stack_names=enable_cdk_recovery_stacks,
            restore_account_credentials=restore_account_credentials,
            regions=vpc_regions
        )

    # Build VPC configs by region for region-specific resource restoration
    vpc_configs_by_region = {}
    for config in vpc_configs:
        region = config.get("region", restore_region)
        vpc_configs_by_region[region] = config

    # Group DynamoDB tables by target region
    dynamodb_tables_by_region = {}
    for snapshot in resource_snapshots:
        if snapshot.get("resourceType") == "AWS_DYNAMO_DB":
            source_region = snapshot.get("region")
            target_region = restore_region if restore_region else source_region

            # Determine actual region (same logic as restore section)
            actual_region = target_region if target_region in vpc_configs_by_region else list(vpc_configs_by_region.keys())[0] if vpc_configs_by_region else None

            if actual_region:
                if actual_region not in dynamodb_tables_by_region:
                    dynamodb_tables_by_region[actual_region] = []

                dynamodb_tables_by_region[actual_region].append({
                    "resourceId": snapshot["resourceId"],
                    "resourceName": snapshot["resourceName"],
                    "sizeBytes": snapshot.get("tableSizeBytes", 0)
                })

    # Calculate WCU allocation per region (95% of regional capacity)
    dynamodb_wcu_allocation = calculate_dynamodb_wcu_allocation_by_region(
        dynamodb_tables_by_region=dynamodb_tables_by_region,
        regional_wcu_capacity=dynamodb_regional_wcu_limit,
        utilization_percentage=0.95,
        default_wcu_for_zero_size=50
    )

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    print(f"\nInitiating restore jobs for {len(resource_snapshots)} snapshots")
    print(f"VPC configurations available in regions: {list(vpc_configs_by_region.keys())}")

    restore_jobs = []

    for resource_snapshot in resource_snapshots:
        resource_id = resource_snapshot["resourceId"]
        resource_name = resource_snapshot["resourceName"]
        resource_type = resource_snapshot["resourceType"]
        snapshot_id = resource_snapshot["snapshotId"]
        snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")
        source_region = resource_snapshot.get("region")

        # Determine target region based on restoreRegion parameter:
        # - If restoreRegion is set → force all resources to that region
        # - If restoreRegion is null → restore to source region
        # - If target region has no VPC config → fall back to any available region
        target_region = restore_region if restore_region else source_region

        print(f"Initiating restore for {resource_type}: {resource_name} (source region: {source_region}, target region: {target_region})")

        # In recoveryStacksOnly mode, skip resource types that don't come from stacks
        if recovery_stacks_only and resource_type in ("AWS_EC2", "AWS_RDS"):
            print(f"SKIPPING {resource_name} ({resource_type}) - recoveryStacksOnly mode, no stack matching for this type")
            continue

        try:
            job_id = None
            restored_resource_details = {}

            if resource_type == "AWS_EC2":
                # Get instance configuration from snapshot
                instance_type = resource_snapshot.get("instanceType", "t3.medium")
                instance_profile_name = resource_snapshot.get("instanceProfileName")
                volumes = resource_snapshot.get("volumes", [])

                # Check if VPC config exists for target region, otherwise fall back to any available
                vpc_config = None
                actual_region = None

                if target_region and target_region in vpc_configs_by_region:
                    actual_region = target_region
                    vpc_config = vpc_configs_by_region[target_region]
                    print(f"Using VPC config for target region: {actual_region}")
                elif vpc_configs_by_region:
                    actual_region = list(vpc_configs_by_region.keys())[0]
                    vpc_config = vpc_configs_by_region[actual_region]
                    print(f"WARNING: No VPC config for target region {target_region}, falling back to: {actual_region}")
                else:
                    raise ValueError(f"No VPC configurations available for restoring {resource_name}")

                # Extract subnets and security groups from the region-specific VPC config
                subnets_per_az = vpc_config.get("subnetsPerAvailabilityZone", [])
                security_groups = vpc_config.get("securityGroups", {})
                security_group_ids = security_groups.get("restoreServer", [])

                if not subnets_per_az:
                    raise ValueError(f"No subnets available in region {actual_region} for EC2 restore of {resource_name}")

                # Create EC2 client for restore account to check instance type availability
                if restore_account_credentials:
                    ec2_client = boto3.client(
                        "ec2",
                        region_name=actual_region,
                        aws_access_key_id=restore_account_credentials["AccessKeyId"],
                        aws_secret_access_key=restore_account_credentials["SecretAccessKey"],
                        aws_session_token=restore_account_credentials["SessionToken"]
                    )
                else:
                    # Fallback to Lambda execution role if no cross-account credentials
                    ec2_client = boto3.client("ec2", region_name=actual_region)

                # Check which AZs support this instance type
                print(f"Checking availability of instance type {instance_type} in region {actual_region}")
                try:
                    offerings_response = ec2_client.describe_instance_type_offerings(
                        LocationType='availability-zone',
                        Filters=[
                            {'Name': 'instance-type', 'Values': [instance_type]}
                        ]
                    )

                    available_azs = {offering['Location'] for offering in offerings_response.get('InstanceTypeOfferings', [])}
                    print(f"Instance type {instance_type} is available in AZs: {sorted(available_azs)}")

                except ClientError as e:
                    print(f"WARNING: Could not check instance type availability: {str(e)}")
                    # If we can't check, proceed with random selection
                    available_azs = None

                # Try to find a subnet in an AZ that supports the instance type
                subnet_id = None
                if available_azs:
                    # Filter subnets to only those in AZs that support the instance type
                    compatible_subnets = [
                        subnet for subnet in subnets_per_az
                        if subnet.get("availabilityZone") in available_azs and subnet.get("subnetId")
                    ]

                    if compatible_subnets:
                        selected_subnet = random.choice(compatible_subnets)
                        subnet_id = selected_subnet["subnetId"]
                        selected_az = selected_subnet["availabilityZone"]
                        print(f"Selected subnet {subnet_id} in AZ {selected_az} (supports {instance_type})")
                    else:
                        # No compatible subnets found
                        configured_azs = {subnet.get("availabilityZone") for subnet in subnets_per_az if subnet.get("availabilityZone")}
                        raise ValueError(
                            f"Instance type {instance_type} is not available in any configured availability zones. "
                            f"Instance type available in: {sorted(available_azs)}, "
                            f"Configured subnets in: {sorted(configured_azs)}"
                        )
                else:
                    # Couldn't check availability, use random selection as fallback
                    available_subnets = [subnet.get("subnetId") for subnet in subnets_per_az if subnet.get("subnetId")]
                    subnet_id = random.choice(available_subnets)
                    print(f"Selected subnet {subnet_id} (could not verify instance type availability)")

                # Volumes must be present - no volumes means no data to restore
                if not volumes:
                    print(f"ERROR: No volume configuration found for {resource_name}, cannot restore")
                    raise ValueError(f"No volumes found for EC2 instance {resource_name}")

                # Get KMS key for this region
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                # Build volume restore parameters from snapshot volume data
                volume_restore_params = []
                for vol in volumes:
                    # Get original volume tags and filter out excluded tag keys
                    original_tags = vol.get("tags", {})
                    filtered_volume_tags = {
                        k: v for k, v in original_tags.items()
                        if k not in exclude_ec2_tag_keys
                    }

                    # Merge filtered original tags with restore tags
                    volume_tags = {
                        **filtered_volume_tags,
                        "eon:restore": "true",
                        "eon:snapshot_id": snapshot_id,
                        "eon:snapshot_time": snapshot_point_in_time
                    }

                    vol_param = {
                        "providerVolumeId": vol.get("providerVolumeId", "unknown"),
                        "volumeEncryptionKeyId": kms_key_arn,  # Encrypt volume with region-specific KMS key
                        "volumeSettings": vol.get("volumeSettings", {}),
                        "tags": volume_tags
                    }
                    volume_restore_params.append(vol_param)

                print(f"EC2 restore config - region: {actual_region}, instance_type: {instance_type}, subnet: {subnet_id}, "
                      f"security_groups: {len(security_group_ids)}, volumes: {len(volume_restore_params)}")
                print(f"Volume encryption - using KMS key: {kms_key_arn}")

                # Get original tags and filter out excluded tag keys
                original_tags = resource_snapshot.get("originalTags", {})
                filtered_original_tags = {
                    k: v for k, v in original_tags.items()
                    if k not in exclude_ec2_tag_keys
                }

                # Log excluded tags if any were filtered
                if exclude_ec2_tag_keys and original_tags:
                    excluded_tags = [k for k in original_tags.keys() if k in exclude_ec2_tag_keys]
                    if excluded_tags:
                        print(f"Excluding EC2 tags for {resource_name}: {excluded_tags}")

                # Merge filtered original tags with restore tags (restore tags take precedence)
                restored_instance_name = get_restored_name(resource_name)
                ec2_tags = {
                    **filtered_original_tags,
                    "Name": restored_instance_name,
                    "RestoreSource": resource_snapshot.get("providerResourceId", ""),
                    "ManagedBy": "EonBulkRecovery",
                    "eon:restore": "true",
                    "eon:snapshot_id": snapshot_id,
                    "eon:snapshot_time": snapshot_point_in_time
                }

                destination_config = {
                    "awsEc2": {
                        "region": actual_region,
                        "instanceType": instance_type,
                        "subnetId": subnet_id,
                        "securityGroupIds": security_group_ids,
                        "tags": ec2_tags,
                        "volumeRestoreParameters": volume_restore_params
                    }
                }

                # Add instance profile if present
                # NOTE: Temporarily commented out as Eon does not currently request permissions to be able to create instance profile in restore account
                # if instance_profile_name:
                #     destination_config["awsEc2"]["instanceProfileName"] = instance_profile_name
                #     print(f"Including instance profile: {instance_profile_name}")

                job_id = eon_client.restore_ec2_instance(
                    resource_id=resource_id,
                    snapshot_id=snapshot_id,
                    restore_account_id=eon_restore_account_id,
                    destination_config=destination_config
                )

                # Capture restored resource details
                restored_resource_details = {
                    "restoredRegion": actual_region,
                    "instanceType": instance_type,
                    "volumeCount": len(volume_restore_params),
                    "restoredName": restored_instance_name
                }

            elif resource_type == "AWS_RDS":
                # Get instance class from source resource, fallback to default
                db_instance_class = resource_snapshot.get("dbInstanceClass", "db.t3.micro")

                # Check if RDS subnet group exists for target region, otherwise fall back to any available
                rds_subnet_group_name = None
                actual_region = None

                if target_region and target_region in rds_subnet_groups_by_region:
                    actual_region = target_region
                    rds_subnet_group_name = rds_subnet_groups_by_region[target_region]
                    print(f"Using RDS subnet group for target region: {actual_region}")
                elif rds_subnet_groups_by_region:
                    actual_region = list(rds_subnet_groups_by_region.keys())[0]
                    rds_subnet_group_name = rds_subnet_groups_by_region[actual_region]
                    print(f"WARNING: No RDS subnet group for target region {target_region}, falling back to: {actual_region}")
                else:
                    raise ValueError(f"No RDS subnet groups available for restoring {resource_name}")

                # Get security groups from the region-specific VPC config
                vpc_config = vpc_configs_by_region.get(actual_region, {})
                security_groups_config = vpc_config.get("securityGroups", {})
                rds_security_groups = security_groups_config.get("restoredRdsInstance", [])

                # Get KMS key for this region
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                # Merge original tags with restore tags (restore tags take precedence)
                original_tags = resource_snapshot.get("originalTags", {})
                restored_db_name = sanitize_rds_identifier(get_restored_name(resource_name))
                rds_tags = {
                    **original_tags,
                    "Name": restored_db_name,
                    "RestoreSource": resource_snapshot.get("providerResourceId", ""),
                    "ManagedBy": "EonBulkRecovery",
                    "eon:restore": "true",
                    "eon:snapshot_id": snapshot_id,
                    "eon:snapshot_time": snapshot_point_in_time
                }

                destination_config = {
                    "awsRds": {
                        "restoreRegion": actual_region,
                        "encryptionKeyId": kms_key_arn,
                        "restoredName": restored_db_name,
                        "securityGroups": rds_security_groups,
                        "subnetGroup": rds_subnet_group_name,
                        "dbInstanceClass": db_instance_class,
                        "tags": rds_tags
                    }
                }

                print(f"RDS restore config - region: {actual_region}, subnet_group: {rds_subnet_group_name}, "
                      f"security_groups: {len(rds_security_groups)}, instance_class: {db_instance_class}")

                job_id = eon_client.restore_rds_instance(
                    resource_id=resource_id,
                    snapshot_id=snapshot_id,
                    restore_account_id=eon_restore_account_id,
                    destination_config=destination_config
                )

                # Capture restored resource details
                restored_resource_details = {
                    "restoredRegion": actual_region,
                    "dbInstanceClass": db_instance_class,
                    "restoredName": restored_db_name,
                    "subnetGroup": rds_subnet_group_name
                }

            elif resource_type == "AWS_S3":
                # Check if VPC config exists for target region, otherwise fall back to any available
                # VPC configs define which regions are allowed for restoration
                actual_region = None

                if target_region and target_region in vpc_configs_by_region:
                    actual_region = target_region
                    print(f"Restoring S3 to target region: {actual_region}")
                elif vpc_configs_by_region:
                    actual_region = list(vpc_configs_by_region.keys())[0]
                    print(f"WARNING: No VPC config for target region {target_region}, falling back to: {actual_region}")
                else:
                    raise ValueError(f"No VPC configurations available for restoring {resource_name}")

                # Get KMS key for this region
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                # Get original tags
                original_tags = resource_snapshot.get("originalTags", {})

                # Check for in-place restore via eon_functional_id tag matching
                stack_bucket_match = None
                source_functional_id = original_tags.get("eon_functional_id")

                if source_functional_id and recovery_stack_s3_buckets:
                    if source_functional_id in recovery_stack_s3_buckets:
                        stack_bucket_match = recovery_stack_s3_buckets[source_functional_id]
                        print(f"Found matching S3 bucket '{stack_bucket_match['bucketName']}' with eon_functional_id='{source_functional_id}' (stack: {stack_bucket_match['stackName']})")

                if stack_bucket_match:
                    # In-place restore to existing bucket from recovery stack
                    restore_bucket_name = stack_bucket_match["bucketName"]
                    restore_bucket_region = stack_bucket_match["region"]

                    # Get KMS key for the bucket's region (used for restore worker EC2 EBS encryption)
                    stack_kms_key_arn = kms_key_arns_by_region.get(restore_bucket_region)
                    if not stack_kms_key_arn:
                        raise ValueError(f"No KMS key available for region {restore_bucket_region} (required for in-place S3 restore)")

                    print(f"S3 IN-PLACE restore config - region: {restore_bucket_region}, bucket: {restore_bucket_name} (CloudFormation stack: {stack_bucket_match['stackName']})")

                    destination_config = {
                        "s3Bucket": {
                            "region": restore_bucket_region,
                            "bucketName": restore_bucket_name,
                            "encryptionKeyId": stack_kms_key_arn,
                            "prefix": ""
                        }
                    }

                    job_id = eon_client.restore_s3_bucket(
                        resource_id=resource_id,
                        snapshot_id=snapshot_id,
                        restore_account_id=eon_restore_account_id,
                        destination_config=destination_config
                    )

                    # Capture restored resource details
                    restored_resource_details = {
                        "restoredRegion": restore_bucket_region,
                        "restoredBucketName": restore_bucket_name,
                        "originalBucketName": resource_snapshot.get("providerResourceId", resource_name),
                        "restoreType": "IN_PLACE",
                        "recoveryStackName": stack_bucket_match["stackName"],
                        "eonFunctionalId": source_functional_id
                    }
                else:
                    if recovery_stacks_only:
                        print(f"SKIPPING {resource_name} (S3) - recoveryStacksOnly mode, no matching stack bucket")
                        continue

                    # Default flow: create a new bucket
                    # Create a bucket name (S3 bucket names must be globally unique)
                    # Include snapshot ID and region in the hash to ensure uniqueness across restores
                    original_bucket_name = resource_snapshot.get("providerResourceId", resource_name)
                    hash_input = f"{original_bucket_name}-{snapshot_id}-{actual_region}-{restore_account_id}"
                    hash_suffix = hashlib.md5(hash_input.encode()).hexdigest()[:8]
                    # For S3, we always need a unique suffix since bucket names are globally unique
                    # When restoring to a new account, the original bucket name likely won't be available
                    if resource_name_prefix:
                        restored_bucket_name = sanitize_s3_bucket_name(f"{resource_name_prefix}{original_bucket_name}", hash_suffix)
                    else:
                        restored_bucket_name = sanitize_s3_bucket_name(original_bucket_name, hash_suffix)

                    print(f"S3 restore config - region: {actual_region}, bucket: {restored_bucket_name}")

                    # Create the S3 bucket first
                    create_s3_bucket(
                        bucket_name=restored_bucket_name,
                        region=actual_region,
                        kms_key_id=kms_key_arn,
                        restore_account_id=restore_account_id,
                        snapshot_id=snapshot_id,
                        snapshot_point_in_time=snapshot_point_in_time,
                        original_tags=original_tags,
                        restore_account_credentials=restore_account_credentials
                    )

                    destination_config = {
                        "s3Bucket": {
                            "region": actual_region,
                            "bucketName": restored_bucket_name,
                            "encryptionKeyId": kms_key_arn,
                            "prefix": ""
                        }
                    }

                    job_id = eon_client.restore_s3_bucket(
                        resource_id=resource_id,
                        snapshot_id=snapshot_id,
                        restore_account_id=eon_restore_account_id,
                        destination_config=destination_config
                    )

                    # Capture restored resource details
                    restored_resource_details = {
                        "restoredRegion": actual_region,
                        "restoredBucketName": restored_bucket_name,
                        "originalBucketName": original_bucket_name
                    }

            elif resource_type == "AWS_DYNAMO_DB":
                # Check if VPC config exists for target region, otherwise fall back to any available
                # VPC configs define which regions are allowed for restoration
                actual_region = None

                if target_region and target_region in vpc_configs_by_region:
                    actual_region = target_region
                    print(f"Restoring DynamoDB to target region: {actual_region}")
                elif vpc_configs_by_region:
                    actual_region = list(vpc_configs_by_region.keys())[0]
                    print(f"WARNING: No VPC config for target region {target_region}, falling back to: {actual_region}")
                else:
                    raise ValueError(f"No VPC configurations available for restoring {resource_name}")

                # Get KMS key for this region (used for restore worker EC2 EBS encryption)
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                # Check if a pre-created table exists for in-place restore
                # Match by table name (source table name) and SOURCE region (not restore region)
                stack_table_match = None
                if recovery_stack_tables:
                    # Look for a pre-created table matching the source table name and SOURCE region
                    if resource_name in recovery_stack_tables:
                        stack_table = recovery_stack_tables[resource_name]
                        if stack_table["region"] == source_region:
                            stack_table_match = stack_table
                            print(f"Found matching pre-created table '{resource_name}' in {source_region} (source region) (stack: {stack_table['stackName']})")

                if stack_table_match:
                    # Use in-place restore to existing pre-created table
                    # The table was created in the same region as the source
                    restore_target_region = stack_table_match["region"]

                    # Get KMS key for the stack table's region (may be different from actual_region)
                    stack_kms_key_arn = kms_key_arns_by_region.get(restore_target_region)
                    if not stack_kms_key_arn:
                        raise ValueError(f"No KMS key available for region {restore_target_region} (required for in-place restore)")

                    print(f"DynamoDB IN-PLACE restore config - region: {restore_target_region}, table: {stack_table_match['tableName']} (CloudFormation stack: {stack_table_match['stackName']})")

                    job_id = eon_client.restore_dynamodb_to_existing_table(
                        resource_id=resource_id,
                        snapshot_id=snapshot_id,
                        restore_account_id=eon_restore_account_id,
                        table_name=stack_table_match["tableName"],
                        region=restore_target_region,
                        encryption_key_id=stack_kms_key_arn  # EBS encryption for restore worker
                    )

                    # Capture restored resource details
                    restored_resource_details = {
                        "restoredRegion": restore_target_region,
                        "restoredName": stack_table_match["tableName"],
                        "restoreType": "IN_PLACE",
                        "recoveryStackName": stack_table_match["stackName"]
                    }
                else:
                    if recovery_stacks_only:
                        print(f"SKIPPING {resource_name} (DynamoDB) - recoveryStacksOnly mode, no matching stack table")
                        continue

                    # Standard restore to new table
                    # Get allocated WCU for this table
                    allocated_wcu = dynamodb_wcu_allocation.get(resource_id, 50)  # fallback to default

                    # Merge original tags with restore tags (restore tags take precedence)
                    original_tags = resource_snapshot.get("originalTags", {})
                    dynamodb_tags = {
                        **original_tags,
                        "ManagedBy": "EonBulkRecovery",
                        "eon:restore": "true",
                        "eon:snapshot_id": snapshot_id,
                        "eon:snapshot_time": snapshot_point_in_time
                    }

                    restored_table_name = get_restored_name(resource_name)
                    print(f"DynamoDB restore config - region: {actual_region}, table: {restored_table_name}, WCU: {allocated_wcu:,}")

                    destination_config = {
                        "awsDynamodb": {
                            "restoreRegion": actual_region,
                            "encryptionKeyId": kms_key_arn,
                            "restoredName": restored_table_name,
                            "writeCapacityUnits": allocated_wcu,
                            "tags": dynamodb_tags
                        }
                    }

                    job_id = eon_client.restore_dynamodb_table(
                        resource_id=resource_id,
                        snapshot_id=snapshot_id,
                        restore_account_id=eon_restore_account_id,
                        destination_config=destination_config
                    )

                    # Capture restored resource details
                    restored_resource_details = {
                        "restoredRegion": actual_region,
                        "restoredName": restored_table_name,
                        "restoreType": "NEW_TABLE",
                        "writeCapacityUnits": allocated_wcu
                    }

            if job_id:
                restore_jobs.append({
                    "jobId": job_id,
                    "resourceId": resource_id,
                    "resourceName": resource_name,
                    "resourceType": resource_type,
                    "snapshotId": snapshot_id,
                    "snapshotPointInTime": snapshot_point_in_time,
                    "sourceRegion": source_region,
                    "status": "INITIATED",
                    **restored_resource_details  # Include region and resource-specific details
                })
                print(f"Successfully initiated restore job {job_id} for {resource_name}")

                # Pause 5 seconds between job initiations to avoid overloading the API
                print("Pausing 5 seconds before next job...")
                time.sleep(5)
            else:
                print(f"WARNING: No job ID returned for {resource_name}")

        except Exception as e:
            print(f"ERROR: Failed to initiate restore for {resource_name}: {str(e)}")
            # Add failed job to the list
            restore_jobs.append({
                "jobId": None,
                "resourceId": resource_id,
                "resourceName": resource_name,
                "resourceType": resource_type,
                "snapshotId": snapshot_id,
                "snapshotPointInTime": snapshot_point_in_time,
                "sourceRegion": source_region,
                "status": "FAILED_TO_INITIATE",
                "error": str(e)
            })
            continue

    print(f"Successfully initiated {len([j for j in restore_jobs if j['jobId']])} restore jobs")

    return {
        "restoreJobs": restore_jobs,
        "totalJobs": len(restore_jobs),
        "successfulJobs": len([j for j in restore_jobs if j["jobId"]]),
        "failedJobs": len([j for j in restore_jobs if not j["jobId"]])
    }
