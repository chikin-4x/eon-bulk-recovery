"""Lambda handler for connecting a restore account to Eon."""

import os
import sys
import time
from typing import Dict, Any
from requests.exceptions import HTTPError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def _reconnect_and_wait(
    eon_client: EonClient,
    eon_restore_account_id: str,
    provider_account_id: str,
    max_attempts: int = 5,
    delay_seconds: int = 10,
) -> str:
    """Trigger a reconnect and poll the account status until it reaches CONNECTED.

    Eon re-validates the restore role's permissions and trust policy on reconnect.
    Because IAM roles/policies the bootstrap step just installed (or repaired) can
    take a few seconds to propagate, we reconnect once and then poll the account a
    handful of times before giving up. Returns the final observed status.
    """
    reconnect_response = eon_client.reconnect_restore_account(eon_restore_account_id)
    status = reconnect_response.get("restoreAccount", {}).get("status", "UNKNOWN")
    print(f"Reconnect requested, status: {status}")

    attempt = 0
    while status != "CONNECTED" and attempt < max_attempts:
        time.sleep(delay_seconds)
        attempt += 1
        list_response = eon_client.list_restore_accounts(provider_account_id=provider_account_id)
        accounts = list_response.get("accounts", [])
        if not accounts:
            print(f"Reconnect poll {attempt}/{max_attempts}: account no longer listed")
            break
        status = accounts[0].get("status", "UNKNOWN")
        print(f"Reconnect poll {attempt}/{max_attempts}: status={status}")

    return status


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

        elif status in ("DISCONNECTED", "INSUFFICIENT_PERMISSIONS"):
            # Both states are recoverable via reconnect: the bootstrap step may
            # have just (re)installed the restore role, so the permissions Eon
            # last saw are stale. Reconnect re-validates them.
            print(f"Restore account status is {status}, attempting to reconnect "
                  f"(bootstrap may have just installed/repaired the restore role)...")
            status = _reconnect_and_wait(eon_client, eon_restore_account_id, restore_account_id)

            if status != "CONNECTED":
                # Roles may still be propagating in AWS IAM. Raise so the Step
                # Functions Retry re-runs this step after a backoff and reconnects
                # again against the (by then more-propagated) role.
                raise ValueError(
                    f"Restore account {restore_account_id} did not reach CONNECTED after reconnect "
                    f"(current status: {status}). Verify the IAM role {role_arn} has the correct "
                    f"permissions and trust policy; the workflow will retry."
                )
            print("Successfully reconnected restore account")

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
