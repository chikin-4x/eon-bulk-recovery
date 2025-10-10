"""AWS utility functions for the bulk recovery workflow."""

import json
import os
from typing import Dict, Any, Optional
import boto3


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
