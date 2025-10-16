"""Lambda handler for initiating restore jobs for all snapshots."""

import os
import sys
import hashlib
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

        # Allocate default WCU to zero-size tables
        zero_size_total_wcu = len(zero_size_tables) * default_wcu_for_zero_size

        for table in zero_size_tables:
            wcu_allocation[table["resourceId"]] = default_wcu_for_zero_size
            print(f"    {table['resourceName']}: 0 GB (no size data) -> {default_wcu_for_zero_size:,} WCU (default)")

        # Remaining WCU for sized tables
        remaining_wcu = available_wcu - zero_size_total_wcu

        if dynamodb_tables and remaining_wcu > 0:
            # Calculate total size across all sized tables in this region
            total_size = sum(table["sizeBytes"] for table in dynamodb_tables)

            print(f"    Total data size (sized tables): {total_size / (1024**3):.2f} GB")
            print(f"    Remaining WCU for sized tables: {remaining_wcu:,}")

            # Allocate WCUs proportionally based on table size
            allocated_total = zero_size_total_wcu

            for table in dynamodb_tables:
                # Calculate proportional WCU
                proportion = table["sizeBytes"] / total_size
                proportional_wcu = int(remaining_wcu * proportion)

                # Ensure minimum of 1 WCU
                allocated_wcu = max(proportional_wcu, 1)

                wcu_allocation[table["resourceId"]] = allocated_wcu
                allocated_total += allocated_wcu

                table_size_gb = table["sizeBytes"] / (1024**3)
                print(f"    {table['resourceName']}: {table_size_gb:.2f} GB ({proportion*100:.1f}%) -> {allocated_wcu:,} WCU")

            print(f"    Total allocated in {region}: {allocated_total:,} WCU ({allocated_total/regional_wcu_capacity*100:.1f}% of regional capacity)")
        elif not dynamodb_tables:
            print(f"    Total allocated in {region}: {zero_size_total_wcu:,} WCU (all tables have zero size)")

    return wcu_allocation


def create_s3_bucket(bucket_name: str, region: str, kms_key_id: str, restore_account_id: str, snapshot_id: str, snapshot_point_in_time: str, original_tags: Dict[str, str] = None, cross_account_role_arn: str = None, management_account_id: str = None) -> None:
    """Create an S3 bucket in the restore account."""
    if original_tags is None:
        original_tags = {}
    # Get credentials for restore account
    credentials = None
    try:
        credentials = get_cross_account_credentials(restore_account_id, cross_account_role_arn, management_account_id)
    except (ValueError, ClientError) as e:
        print(f"Could not get cross-account credentials for S3 bucket creation: {str(e)}")
        print("Falling back to Lambda execution role credentials")

    if credentials:
        s3_client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"]
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

        # Merge original tags with restore tags (restore tags take precedence)
        s3_tags = {
            **original_tags,
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

    print(f"KMS keys available in regions: {list(kms_key_arns_by_region.keys())}")
    print(f"RDS subnet groups available in regions: {list(rds_subnet_groups_by_region.keys())}")
    print(f"DynamoDB regional WCU limit: {dynamodb_regional_wcu_limit:,}")

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

        try:
            job_id = None

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
                available_subnets = [subnet.get("subnetId") for subnet in subnets_per_az if subnet.get("subnetId")]
                security_groups = vpc_config.get("securityGroups", {})
                security_group_ids = security_groups.get("restoreServer", [])

                if not available_subnets:
                    raise ValueError(f"No subnets available in region {actual_region} for EC2 restore of {resource_name}")

                # Randomly select a subnet from available subnets in this region for load distribution
                subnet_id = random.choice(available_subnets)

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
                    # Preserve original volume tags and add eon tags
                    original_tags = vol.get("tags", {})
                    volume_tags = {
                        **original_tags,
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

                # Merge original tags with restore tags (restore tags take precedence)
                original_tags = resource_snapshot.get("originalTags", {})
                ec2_tags = {
                    **original_tags,
                    "Name": f"restored-{resource_name}",
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
                rds_tags = {
                    **original_tags,
                    "Name": f"restored-{resource_name}",
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
                        "restoredName": f"restored-{resource_name}",
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

                # Create a bucket name (S3 bucket names must be globally unique)
                # Include snapshot ID and region in the hash to ensure uniqueness across restores
                original_bucket_name = resource_snapshot.get("providerResourceId", resource_name)
                hash_input = f"{original_bucket_name}-{snapshot_id}-{actual_region}-{restore_account_id}"
                hash_suffix = hashlib.md5(hash_input.encode()).hexdigest()[:8]
                restored_bucket_name = f"restored-{original_bucket_name}-{hash_suffix}".lower()[:63]

                # Get KMS key for this region
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                print(f"S3 restore config - region: {actual_region}, bucket: {restored_bucket_name}")

                # Get original tags
                original_tags = resource_snapshot.get("originalTags", {})

                # Create the S3 bucket first
                create_s3_bucket(
                    bucket_name=restored_bucket_name,
                    region=actual_region,
                    kms_key_id=kms_key_arn,
                    restore_account_id=restore_account_id,
                    snapshot_id=snapshot_id,
                    snapshot_point_in_time=snapshot_point_in_time,
                    original_tags=original_tags,
                    cross_account_role_arn=cross_account_role_arn,
                    management_account_id=management_account_id
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

                # Get allocated WCU for this table
                allocated_wcu = dynamodb_wcu_allocation.get(resource_id, 50)  # fallback to default

                # Get KMS key for this region
                kms_key_arn = kms_key_arns_by_region.get(actual_region)
                if not kms_key_arn:
                    raise ValueError(f"No KMS key available for region {actual_region}")

                # Merge original tags with restore tags (restore tags take precedence)
                original_tags = resource_snapshot.get("originalTags", {})
                dynamodb_tags = {
                    **original_tags,
                    "ManagedBy": "EonBulkRecovery",
                    "eon:restore": "true",
                    "eon:snapshot_id": snapshot_id,
                    "eon:snapshot_time": snapshot_point_in_time
                }

                print(f"DynamoDB restore config - region: {actual_region}, table: restored-{resource_name}, WCU: {allocated_wcu:,}")

                destination_config = {
                    "awsDynamodb": {
                        "restoreRegion": actual_region,
                        "encryptionKeyId": kms_key_arn,
                        "restoredName": f"restored-{resource_name}",
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

            if job_id:
                restore_jobs.append({
                    "jobId": job_id,
                    "resourceId": resource_id,
                    "resourceName": resource_name,
                    "resourceType": resource_type,
                    "snapshotId": snapshot_id,
                    "status": "INITIATED"
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
