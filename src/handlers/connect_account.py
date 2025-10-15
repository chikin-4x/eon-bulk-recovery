"""Lambda handler for connecting a restore account to Eon."""

import os
import sys
from typing import Dict, Any
from requests.exceptions import HTTPError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Connect a restore account to Eon via the REST API.

    Input event:
        roleArn: ARN of the IAM role created in the bootstrap step
        restoreAccountName: Display name for the restore account in Eon
        restoreAccountId: AWS account ID of the restore account

    Returns:
        eonRestoreAccountId: Eon-assigned ID for the restore account
        restoreAccountName: Name of the restore account
        roleArn: ARN of the IAM role
    """
    role_arn = event["roleArn"]
    restore_account_id = event["restoreAccountId"]
    restore_account_name = event.get("restoreAccountName")

    # If restoreAccountName is null or not provided, auto-generate it
    if not restore_account_name:
        restore_account_name = f"bulk-recovery-{restore_account_id}"

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # Try to connect the restore account
    print(f"Attempting to connect restore account: {restore_account_name} (ID: {restore_account_id})")

    eon_restore_account_id = None
    status = None

    try:
        response = eon_client.connect_restore_account(
            name=restore_account_name,
            role_arn=role_arn
        )

        restore_account = response.get("restoreAccount", {})
        eon_restore_account_id = restore_account.get("id")
        status = restore_account.get("status", "UNKNOWN")

        print(f"Successfully connected restore account. Eon ID: {eon_restore_account_id}")

    except HTTPError as e:
        # If connect fails, check if the account already exists
        print(f"Connect failed (status {e.response.status_code}), checking if restore account already exists...")

        # List restore accounts filtering by provider account ID
        list_response = eon_client.list_restore_accounts(
            provider_account_id=restore_account_id
        )

        accounts = list_response.get("accounts", [])

        if not accounts:
            print(f"No existing restore account found for AWS account {restore_account_id}")
            raise

        # Use the first matching account
        existing_account = accounts[0]
        eon_restore_account_id = existing_account.get("id")
        status = existing_account.get("status")

        print(f"Found existing restore account. Eon ID: {eon_restore_account_id}, Status: {status}")

        if status == "CONNECTED":
            print("Restore account is already connected, using existing account")

        elif status == "DISCONNECTED":
            print("Restore account is disconnected, attempting to reconnect...")
            reconnect_response = eon_client.reconnect_restore_account(eon_restore_account_id)
            restored_account = reconnect_response.get("restoreAccount", {})
            status = restored_account.get("status", "UNKNOWN")
            print(f"Successfully reconnected restore account. New status: {status}")

        elif status == "INSUFFICIENT_PERMISSIONS":
            raise ValueError(
                f"Restore account {restore_account_id} exists but has INSUFFICIENT_PERMISSIONS status. "
                f"Please verify the IAM role {role_arn} has correct permissions and trust policy."
            )

        else:
            print(f"Warning: Restore account has unexpected status: {status}")

    if not eon_restore_account_id:
        raise ValueError("Failed to retrieve Eon restore account ID")

    return {
        "eonRestoreAccountId": eon_restore_account_id,
        "restoreAccountName": restore_account_name,
        "roleArn": role_arn,
        "restoreAccountId": restore_account_id,
        "status": status
    }
