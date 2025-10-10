"""Lambda handler for connecting a restore account to Eon."""

import os
import sys
from typing import Dict, Any

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
    restore_account_name = event.get("restoreAccountName", f"restore-{event['restoreAccountId']}")
    restore_account_id = event["restoreAccountId"]

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # Connect the restore account
    print(f"Connecting restore account: {restore_account_name} (ID: {restore_account_id})")

    response = eon_client.connect_restore_account(
        name=restore_account_name,
        role_arn=role_arn
    )

    restore_account = response.get("restoreAccount", {})
    eon_restore_account_id = restore_account.get("id")

    if not eon_restore_account_id:
        raise ValueError("Failed to retrieve Eon restore account ID from response")

    print(f"Successfully connected restore account. Eon ID: {eon_restore_account_id}")

    return {
        "eonRestoreAccountId": eon_restore_account_id,
        "restoreAccountName": restore_account_name,
        "roleArn": role_arn,
        "restoreAccountId": restore_account_id,
        "status": restore_account.get("status", "UNKNOWN")
    }
