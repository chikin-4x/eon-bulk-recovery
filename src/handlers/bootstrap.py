"""Lambda handler for bootstrapping the restore account."""

import os
import sys
from typing import Dict, Any
import boto3
import requests
from botocore.exceptions import ClientError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.aws_utils import get_cross_account_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Bootstrap the restore account with necessary IAM permissions, RDS subnet groups, and KMS keys.

    Input event:
        restoreAccountId: AWS account ID of the restore account
        restoreRegion: Primary region for the restore (default: us-east-1)
        vpcConfigs: List of VPC configurations (optional)
            Each config contains:
                region: AWS region
                vpc: VPC ID
                subnetsPerAvailabilityZone: List of {availabilityZone, subnetId}
        crossAccountRoleArn: ARN of role to assume in restore account (optional)

    Returns:
        roleArn: ARN of the created Eon restore role
        kmsKeyArnsByRegion: Dictionary mapping region to KMS key ARN
        rdsSubnetGroupsByRegion: Dictionary mapping region to RDS subnet group name
        dynamodbAccountWcuQuota: Account-level DynamoDB WCU quota
        dynamodbPerTableWcuQuota: Per-table DynamoDB WCU quota
    """
    restore_account_id = event["restoreAccountId"]
    restore_region = event.get("restoreRegion", "us-east-1")
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")
    eon_account_id = os.environ["EON_ACCOUNT_ID"]
    management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip()

    # Get credentials for restore account with role chaining support
    credentials = get_cross_account_credentials(
        restore_account_id=restore_account_id,
        cross_account_role_arn=cross_account_role_arn,
        management_account_id=management_account_id if management_account_id else None
    )

    # Create AWS clients using the cross-account credentials
    cfn_client = boto3.client(
        "cloudformation",
        region_name=restore_region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )
    kms_client = boto3.client(
        "kms",
        region_name=restore_region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )
    service_quotas_client = boto3.client(
        "service-quotas",
        region_name=restore_region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )

    # 1. Deploy IAM CloudFormation stack
    stack_name = f"eon-restore-account-{restore_account_id}"

    # Fetch the latest version of the restore account template
    template_url = "https://eon-public-b2b628cc-1d96-4fda-8dae-c3b1ad3ea03b.s3.amazonaws.com/restore-account.yml"
    print(f"Fetching latest restore account template from: {template_url}")

    template_response = requests.get(template_url)
    template_response.raise_for_status()
    template_body = template_response.text

    print(f"Deploying IAM CloudFormation stack: {stack_name}")

    try:
        cfn_client.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=[
                {
                    "ParameterKey": "EonAccountId",
                    "ParameterValue": eon_account_id
                }
            ],
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=[
                {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
                {"Key": "RestoreAccountId", "Value": restore_account_id}
            ]
        )

        # Wait for stack creation to complete
        waiter = cfn_client.get_waiter("stack_create_complete")
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 60}
        )

        # Get stack outputs
        response = cfn_client.describe_stacks(StackName=stack_name)
        outputs = response["Stacks"][0].get("Outputs", [])
        role_arn = None

        for output in outputs:
            if output["OutputKey"] == "EonRestoreAccountRoleArn":
                role_arn = output["OutputValue"]
                break

        if not role_arn:
            # If no output, construct the ARN manually
            role_arn = f"arn:aws:iam::{restore_account_id}:role/EonRestoreAccountRole"

    except ClientError as e:
        if "AlreadyExistsException" in str(e):
            print(f"Stack {stack_name} already exists, retrieving role ARN")
            response = cfn_client.describe_stacks(StackName=stack_name)
            outputs = response["Stacks"][0].get("Outputs", [])
            role_arn = None
            for output in outputs:
                if output["OutputKey"] == "EonRestoreAccountRoleArn":
                    role_arn = output["OutputValue"]
                    break
            if not role_arn:
                role_arn = f"arn:aws:iam::{restore_account_id}:role/EonRestoreAccountRole"
        else:
            raise

    # 2. Create RDS subnet groups for all regions in VPC configs
    rds_subnet_groups_by_region = {}

    if vpc_configs:
        print(f"Creating RDS subnet groups for {len(vpc_configs)} regions")

        for config in vpc_configs:
            region = config.get("region", restore_region)
            vpc_id = config.get("vpc")
            subnets_per_az = config.get("subnetsPerAvailabilityZone", [])
            subnet_ids = [subnet["subnetId"] for subnet in subnets_per_az if "subnetId" in subnet]

            if not subnet_ids:
                print(f"WARNING: No subnets found for region {region}, skipping RDS subnet group creation")
                continue

            # Create RDS client for this region
            rds_client = boto3.client(
                "rds",
                region_name=region,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"]
            )

            subnet_group_name = f"eon-restore-{restore_account_id}-{region}"
            print(f"Creating RDS subnet group in {region}: {subnet_group_name}")

            try:
                rds_client.create_db_subnet_group(
                    DBSubnetGroupName=subnet_group_name,
                    DBSubnetGroupDescription=f"Eon bulk recovery subnet group for account {restore_account_id} in {region}",
                    SubnetIds=subnet_ids,
                    Tags=[
                        {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
                        {"Key": "RestoreAccountId", "Value": restore_account_id},
                        {"Key": "Region", "Value": region}
                    ]
                )
                print(f"Successfully created RDS subnet group in {region}")
            except ClientError as e:
                if "DBSubnetGroupAlreadyExists" in str(e):
                    print(f"RDS subnet group {subnet_group_name} already exists in {region}")
                else:
                    print(f"ERROR: Failed to create RDS subnet group in {region}: {str(e)}")
                    raise

            rds_subnet_groups_by_region[region] = subnet_group_name

    # 3. Query DynamoDB WCU quotas
    print("Querying DynamoDB write capacity unit (WCU) quotas")

    # Default quotas if API calls fail
    account_wcu_quota = 80000  # Default account-level WCU quota
    per_table_wcu_quota = 40000  # Default per-table WCU quota

    try:
        # Query account-level WCU quota (L-F98FE922)
        # This quota covers both RCU and WCU combined for provisioned mode
        account_quota_response = service_quotas_client.get_service_quota(
            ServiceCode='dynamodb',
            QuotaCode='L-F98FE922'
        )
        # The quota value is for combined RCU+WCU, but we'll use it as WCU limit
        account_wcu_quota = int(account_quota_response['Quota']['Value'])
        print(f"Account-level DynamoDB WCU quota: {account_wcu_quota}")
    except ClientError as e:
        print(f"WARNING: Could not retrieve account-level WCU quota, using default {account_wcu_quota}: {str(e)}")

    try:
        # Query per-table WCU quota (L-1F83C0DA)
        per_table_quota_response = service_quotas_client.get_service_quota(
            ServiceCode='dynamodb',
            QuotaCode='L-1F83C0DA'
        )
        per_table_wcu_quota = int(per_table_quota_response['Quota']['Value'])
        print(f"Per-table DynamoDB WCU quota: {per_table_wcu_quota}")
    except ClientError as e:
        print(f"WARNING: Could not retrieve per-table WCU quota, using default {per_table_wcu_quota}: {str(e)}")

    # 4. Create KMS keys for encryption in each region
    kms_key_arns_by_region = {}

    # Collect all unique regions from VPC configs
    regions_to_setup = set()
    if vpc_configs:
        for config in vpc_configs:
            region = config.get("region", restore_region)
            regions_to_setup.add(region)
    else:
        # If no VPC configs, create key in restore_region
        regions_to_setup.add(restore_region)

    print(f"Creating KMS keys for encryption in {len(regions_to_setup)} region(s)")

    for region in regions_to_setup:
        print(f"Creating KMS key in {region}")

        # Create KMS client for this region
        region_kms_client = boto3.client(
            "kms",
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"]
        )

        alias_name = f"alias/eon-restore-{restore_account_id}-{region}"

        try:
            key_response = region_kms_client.create_key(
                Description=f"Eon bulk recovery encryption key for account {restore_account_id} in {region}",
                KeyUsage="ENCRYPT_DECRYPT",
                Origin="AWS_KMS",
                MultiRegion=False,
                Tags=[
                    {"TagKey": "ManagedBy", "TagValue": "EonBulkRecovery"},
                    {"TagKey": "RestoreAccountId", "TagValue": restore_account_id},
                    {"TagKey": "Region", "TagValue": region}
                ]
            )
            kms_key_arn = key_response["KeyMetadata"]["Arn"]
            kms_key_id = key_response["KeyMetadata"]["KeyId"]

            # Create alias for easier identification
            try:
                region_kms_client.create_alias(
                    AliasName=alias_name,
                    TargetKeyId=kms_key_id
                )
                print(f"Successfully created KMS key in {region}: {kms_key_arn}")
            except ClientError as e:
                if "AlreadyExistsException" in str(e):
                    print(f"KMS alias {alias_name} already exists in {region}")
                else:
                    raise

            kms_key_arns_by_region[region] = kms_key_arn

        except ClientError as e:
            # If we hit any error, try to find existing key by alias
            try:
                alias_response = region_kms_client.describe_key(KeyId=alias_name)
                kms_key_arn = alias_response["KeyMetadata"]["Arn"]
                print(f"Using existing KMS key in {region}: {kms_key_arn}")
                kms_key_arns_by_region[region] = kms_key_arn
            except:
                print(f"ERROR: Failed to create or find KMS key in {region}: {str(e)}")
                raise e

    result = {
        "roleArn": role_arn,
        "kmsKeyArnsByRegion": kms_key_arns_by_region,
        "restoreAccountId": restore_account_id,
        "restoreRegion": restore_region,
        "rdsSubnetGroupsByRegion": rds_subnet_groups_by_region,
        "dynamodbAccountWcuQuota": account_wcu_quota,
        "dynamodbPerTableWcuQuota": per_table_wcu_quota
    }

    print(f"Bootstrap complete. Created KMS keys in {len(kms_key_arns_by_region)} regions, RDS subnet groups in {len(rds_subnet_groups_by_region)} regions")

    return result
