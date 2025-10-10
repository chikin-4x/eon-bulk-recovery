"""Lambda handler for configuring VPC connectivity for the restore account."""

import os
import sys
from typing import Dict, Any, List

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Configure VPC connectivity for a restore account in Eon.

    Input event:
        eonRestoreAccountId: Eon-assigned restore account ID
        vpcConfigs: List of VPC configurations
            Each config contains:
                region: AWS region
                vpc: VPC ID
                subnetsPerAvailabilityZone: List of {availabilityZone, subnetId}
                securityGroups: {restoreServer: [sg-id], restoredRdsInstance: [sg-id]}

    Returns:
        status: Success or failure status
        eonRestoreAccountId: The restore account ID
    """
    eon_restore_account_id = event["eonRestoreAccountId"]
    vpc_configs = event.get("vpcConfigs", [])

    if not vpc_configs:
        print("No VPC configs provided, skipping VPC configuration")
        return {
            "status": "SKIPPED",
            "eonRestoreAccountId": eon_restore_account_id,
            "message": "No VPC configuration provided"
        }

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # Configure VPC connectivity
    print(f"Configuring VPC connectivity for restore account: {eon_restore_account_id}")
    print(f"VPC configs: {vpc_configs}")

    response = eon_client.configure_vpc_connectivity(
        restore_account_id=eon_restore_account_id,
        vpc_configs=vpc_configs
    )

    print(f"Successfully configured VPC connectivity")

    return {
        "status": "SUCCESS",
        "eonRestoreAccountId": eon_restore_account_id,
        "vpcConfigsApplied": len(vpc_configs)
    }
