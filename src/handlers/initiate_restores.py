"""Lambda handler for initiating restore jobs for all snapshots."""

import os
import sys
import hashlib
from typing import Dict, Any, List
import boto3
from botocore.exceptions import ClientError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def create_s3_bucket(bucket_name: str, region: str, kms_key_id: str, cross_account_role_arn: str = None) -> None:
    """Create an S3 bucket in the restore account."""
    if cross_account_role_arn:
        sts_client = boto3.client("sts")
        response = sts_client.assume_role(
            RoleArn=cross_account_role_arn,
            RoleSessionName="EonBulkRecoveryS3"
        )
        credentials = response["Credentials"]
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

        # Add tags
        s3_client.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={
                "TagSet": [
                    {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
                    {"Key": "Purpose", "Value": "RestoreDestination"}
                ]
            }
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
        kmsKeyArn: KMS key ARN for encryption
        rdsSubnetGroupName: RDS subnet group name (if applicable)
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
    kms_key_arn = event.get("kmsKeyArn")
    rds_subnet_group_name = event.get("rdsSubnetGroupName")
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    print(f"Initiating restore jobs for {len(resource_snapshots)} snapshots")

    restore_jobs = []

    # Get default VPC config if available
    default_vpc_config = vpc_configs[0] if vpc_configs else {}
    default_subnet = None
    default_security_groups = []

    if default_vpc_config:
        subnets_per_az = default_vpc_config.get("subnetsPerAvailabilityZone", [])
        if subnets_per_az:
            default_subnet = subnets_per_az[0].get("subnetId")

        security_groups = default_vpc_config.get("securityGroups", {})
        default_security_groups = security_groups.get("restoreServer", [])

    for resource_snapshot in resource_snapshots:
        resource_id = resource_snapshot["resourceId"]
        resource_name = resource_snapshot["resourceName"]
        resource_type = resource_snapshot["resourceType"]
        snapshot_id = resource_snapshot["snapshotId"]
        resource_region = resource_snapshot.get("region", restore_region)

        print(f"Initiating restore for {resource_type}: {resource_name}")

        try:
            job_id = None

            if resource_type == "AWS_EC2":
                # Get instance type from source resource, fallback to default
                instance_type = resource_snapshot.get("instanceType", "t3.medium")

                destination_config = {
                    "awsEc2": {
                        "region": resource_region,
                        "instanceType": instance_type,
                        "subnetId": default_subnet or "subnet-default",
                        "securityGroupIds": default_security_groups,
                        "tags": {
                            "Name": f"restored-{resource_name}",
                            "RestoreSource": resource_snapshot.get("providerResourceId", ""),
                            "ManagedBy": "EonBulkRecovery"
                        },
                        "volumeRestoreParameters": [
                            {
                                "providerVolumeId": "vol-root",  # This should be extracted from snapshot metadata
                                "description": "Root volume",
                                "volumeEncryptionKeyId": kms_key_arn,
                                "volumeSettings": {
                                    "type": "gp3",
                                    "sizeBytes": 10737418240,  # 10 GB default
                                    "iops": 3000,
                                    "throughput": 125
                                }
                            }
                        ]
                    }
                }

                job_id = eon_client.restore_ec2_instance(
                    resource_id=resource_id,
                    snapshot_id=snapshot_id,
                    restore_account_id=eon_restore_account_id,
                    destination_config=destination_config
                )

            elif resource_type == "AWS_RDS":
                # Get instance class from source resource, fallback to default
                db_instance_class = resource_snapshot.get("dbInstanceClass", "db.t3.micro")

                destination_config = {
                    "awsRds": {
                        "restoreRegion": resource_region,
                        "encryptionKeyId": kms_key_arn,
                        "restoredName": f"restored-{resource_name}",
                        "securityGroups": default_security_groups,
                        "subnetGroup": rds_subnet_group_name,
                        "dbInstanceClass": db_instance_class,
                        "tags": {
                            "Name": f"restored-{resource_name}",
                            "RestoreSource": resource_snapshot.get("providerResourceId", ""),
                            "ManagedBy": "EonBulkRecovery"
                        }
                    }
                }

                job_id = eon_client.restore_rds_instance(
                    resource_id=resource_id,
                    snapshot_id=snapshot_id,
                    restore_account_id=eon_restore_account_id,
                    destination_config=destination_config
                )

            elif resource_type == "AWS_S3":
                # Create a bucket name (S3 bucket names must be globally unique)
                original_bucket_name = resource_snapshot.get("providerResourceId", resource_name)
                # Create unique bucket name by appending account ID and hash
                hash_suffix = hashlib.md5(f"{original_bucket_name}-{restore_account_id}".encode()).hexdigest()[:8]
                restored_bucket_name = f"restored-{original_bucket_name}-{hash_suffix}".lower()[:63]

                # Create the S3 bucket first
                create_s3_bucket(
                    bucket_name=restored_bucket_name,
                    region=resource_region,
                    kms_key_id=kms_key_arn,
                    cross_account_role_arn=cross_account_role_arn
                )

                destination_config = {
                    "s3Bucket": {
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
                destination_config = {
                    "awsDynamodb": {
                        "restoreRegion": resource_region,
                        "encryptionKeyId": kms_key_arn,
                        "restoredName": f"restored-{resource_name}",
                        "writeCapacityUnits": 40000,  # Default write capacity
                        "tags": {
                            "Name": f"restored-{resource_name}",
                            "RestoreSource": resource_snapshot.get("providerResourceId", ""),
                            "ManagedBy": "EonBulkRecovery"
                        }
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
