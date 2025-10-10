"""Lambda handler for bootstrapping the restore account."""

import os
from typing import Dict, Any
import boto3
import requests
from botocore.exceptions import ClientError


def get_cross_account_credentials(restore_account_id: str, cross_account_role_arn: str = None, management_account_id: str = None) -> Dict[str, str]:
    """
    Get credentials for the restore account with support for role chaining.

    Tries in this order:
    1. Provided cross-account role ARN (if given)
    2. Role chaining through management account (if management_account_id provided)
    3. Direct AWS Organizations OrganizationAccountAccessRole access

    Args:
        restore_account_id: AWS account ID of the restore account
        cross_account_role_arn: Optional explicit role ARN to assume
        management_account_id: Optional AWS Organizations management account ID for role chaining

    Returns:
        Dictionary with AccessKeyId, SecretAccessKey, and SessionToken

    Raises:
        ValueError: If no valid cross-account access method is available
    """
    sts_client = boto3.client("sts")

    # If explicit role ARN provided, use it
    if cross_account_role_arn:
        print(f"Using provided cross-account role: {cross_account_role_arn}")
        try:
            response = sts_client.assume_role(
                RoleArn=cross_account_role_arn,
                RoleSessionName="EonBulkRecoveryBootstrap"
            )
            return response["Credentials"]
        except ClientError as e:
            raise ValueError(f"Failed to assume provided role {cross_account_role_arn}: {str(e)}")

    # If management account ID provided, use role chaining
    if management_account_id:
        print(f"Using role chaining through management account: {management_account_id}")

        # Step 1: Assume role in management account
        mgmt_role_arn = f"arn:aws:iam::{management_account_id}:role/EonBulkRecoveryChainRole"
        print(f"Step 1: Assuming role in management account: {mgmt_role_arn}")

        try:
            mgmt_response = sts_client.assume_role(
                RoleArn=mgmt_role_arn,
                RoleSessionName="EonBulkRecoveryChain"
            )
            mgmt_credentials = mgmt_response["Credentials"]
            print("Successfully assumed management account role")

            # Step 2: Use management account credentials to assume OrganizationAccountAccessRole
            mgmt_sts_client = boto3.client(
                "sts",
                aws_access_key_id=mgmt_credentials["AccessKeyId"],
                aws_secret_access_key=mgmt_credentials["SecretAccessKey"],
                aws_session_token=mgmt_credentials["SessionToken"]
            )

            org_role_arn = f"arn:aws:iam::{restore_account_id}:role/OrganizationAccountAccessRole"
            print(f"Step 2: Assuming OrganizationAccountAccessRole in restore account: {org_role_arn}")

            org_response = mgmt_sts_client.assume_role(
                RoleArn=org_role_arn,
                RoleSessionName="EonBulkRecoveryBootstrap"
            )
            print("Successfully assumed OrganizationAccountAccessRole via role chaining")
            return org_response["Credentials"]

        except ClientError as e:
            error_msg = str(e)
            if "EonBulkRecoveryChainRole" in error_msg:
                raise ValueError(
                    f"Failed to assume EonBulkRecoveryChainRole in management account {management_account_id}. "
                    f"Please ensure the role exists and trusts this account. See README for setup instructions."
                )
            elif "OrganizationAccountAccessRole" in error_msg:
                raise ValueError(
                    f"Failed to assume OrganizationAccountAccessRole in restore account {restore_account_id}. "
                    f"Ensure the restore account is part of your AWS Organization."
                )
            raise ValueError(f"Role chaining failed: {error_msg}")

    # Try direct OrganizationAccountAccessRole access (management account deployment)
    org_role_arn = f"arn:aws:iam::{restore_account_id}:role/OrganizationAccountAccessRole"
    print(f"No cross-account role or management account provided. Attempting direct access to: {org_role_arn}")

    try:
        response = sts_client.assume_role(
            RoleArn=org_role_arn,
            RoleSessionName="EonBulkRecoveryBootstrap"
        )
        print("Successfully assumed OrganizationAccountAccessRole")
        return response["Credentials"]
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ["AccessDenied", "NoSuchEntity"]:
            raise ValueError(
                f"Cannot access restore account {restore_account_id}. "
                f"Options: "
                f"(1) Deploy in Organization Management Account, "
                f"(2) Provide ManagementAccountId parameter for role chaining, "
                f"(3) Provide crossAccountRoleArn parameter with a custom role."
            )
        raise


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Bootstrap the restore account with necessary IAM permissions, RDS subnet group, and KMS key.

    Input event:
        restoreAccountId: AWS account ID of the restore account
        restoreRegion: Primary region for the restore (default: us-east-1)
        vpcId: VPC ID for RDS subnet group
        subnetIds: List of subnet IDs for RDS subnet group
        crossAccountRoleArn: ARN of role to assume in restore account (optional)

    Returns:
        roleArn: ARN of the created Eon restore role
        rdsSubnetGroupName: Name of the created RDS subnet group
        kmsKeyArn: ARN of the created KMS key
    """
    restore_account_id = event["restoreAccountId"]
    restore_region = event.get("restoreRegion", "us-east-1")
    vpc_id = event.get("vpcId")
    subnet_ids = event.get("subnetIds", [])
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
    rds_client = boto3.client(
        "rds",
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

    # 2. Create RDS subnet group (if VPC and subnets are provided)
    rds_subnet_group_name = None
    if vpc_id and subnet_ids:
        rds_subnet_group_name = f"eon-restore-subnet-group-{restore_account_id}"
        print(f"Creating RDS subnet group: {rds_subnet_group_name}")

        try:
            rds_client.create_db_subnet_group(
                DBSubnetGroupName=rds_subnet_group_name,
                DBSubnetGroupDescription=f"Eon bulk recovery subnet group for account {restore_account_id}",
                SubnetIds=subnet_ids,
                Tags=[
                    {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
                    {"Key": "RestoreAccountId", "Value": restore_account_id}
                ]
            )
        except ClientError as e:
            if "DBSubnetGroupAlreadyExists" in str(e):
                print(f"RDS subnet group {rds_subnet_group_name} already exists")
            else:
                raise

    # 3. Create KMS key for encryption
    print("Creating KMS key for restored resources")

    try:
        key_response = kms_client.create_key(
            Description=f"Eon bulk recovery encryption key for account {restore_account_id}",
            KeyUsage="ENCRYPT_DECRYPT",
            Origin="AWS_KMS",
            MultiRegion=False,
            Tags=[
                {"TagKey": "ManagedBy", "TagValue": "EonBulkRecovery"},
                {"TagKey": "RestoreAccountId", "TagValue": restore_account_id}
            ]
        )
        kms_key_arn = key_response["KeyMetadata"]["Arn"]
        kms_key_id = key_response["KeyMetadata"]["KeyId"]

        # Create alias for easier identification
        alias_name = f"alias/eon-restore-{restore_account_id}"
        try:
            kms_client.create_alias(
                AliasName=alias_name,
                TargetKeyId=kms_key_id
            )
        except ClientError as e:
            if "AlreadyExistsException" in str(e):
                print(f"KMS alias {alias_name} already exists")
            else:
                raise

    except ClientError as e:
        # If we hit any error, try to find existing key by alias
        alias_name = f"alias/eon-restore-{restore_account_id}"
        try:
            alias_response = kms_client.describe_key(KeyId=alias_name)
            kms_key_arn = alias_response["KeyMetadata"]["Arn"]
            print(f"Using existing KMS key: {kms_key_arn}")
        except:
            raise e

    result = {
        "roleArn": role_arn,
        "kmsKeyArn": kms_key_arn,
        "restoreAccountId": restore_account_id,
        "restoreRegion": restore_region
    }

    if rds_subnet_group_name:
        result["rdsSubnetGroupName"] = rds_subnet_group_name

    return result
