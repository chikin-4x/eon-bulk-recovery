"""AWS utility functions for the bulk recovery workflow."""

import json
import os
from typing import Dict, Any, Optional
import boto3
from botocore.exceptions import ClientError


def get_eon_credentials() -> Dict[str, str]:
    """
    Retrieve Eon API credentials from Secrets Manager.

    Returns:
        Dictionary containing clientId and clientSecret
    """
    secret_name = os.environ["EON_CREDENTIALS_SECRET_ARN"]

    secrets_client = boto3.client("secretsmanager")
    response = secrets_client.get_secret_value(SecretId=secret_name)

    return json.loads(response["SecretString"])


def assume_role(role_arn: str, session_name: str = "EonBulkRecovery") -> boto3.Session:
    """
    Assume an IAM role and return a session.

    Args:
        role_arn: ARN of the role to assume
        session_name: Name for the assumed role session

    Returns:
        Boto3 session with assumed role credentials
    """
    sts_client = boto3.client("sts")

    response = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name
    )

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )


def get_account_id() -> str:
    """Get the current AWS account ID."""
    sts_client = boto3.client("sts")
    return sts_client.get_caller_identity()["Account"]


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
