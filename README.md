# Eon Bulk Recovery Application

Automated disaster recovery application for AWS resources backed up by Eon. Uses AWS Step Functions to orchestrate complete multi-region restore workflows.

![Workflow](./screenshot_statemachine.png)

## Quick Start

### Prerequisites

**Eon Requirements:**
- Eon account with API credentials (Client ID and Secret)
- Project ID and Account ID

**AWS Requirements:**
- AWS CLI and SAM CLI installed (`pip install aws-sam-cli`)
- IAM permissions to deploy CloudFormation stacks
- Restore account with cross-account access (see below)

### Cross-Account Setup

Choose one option based on your AWS setup:

#### Option A: AWS Organizations (Recommended)

1. Deploy the main application with `ManagementAccountId` parameter
2. Deploy the chain role in your management account:

```bash
aws cloudformation deploy \
  --template-file management-account-role.yaml \
  --stack-name eon-bulk-recovery-chain-role \
  --parameter-overrides BackupAccountId=<YOUR_BACKUP_ACCOUNT_ID> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

This enables automatic role chaining: Lambda → Chain Role → OrganizationAccountAccessRole

#### Option B: Manual Cross-Account Role

For non-Organization accounts, create an IAM role in the restore account with:

**Trust policy:**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::<BACKUP_ACCOUNT>:role/EonBulkRecoveryLambdaRole"},
    "Action": "sts:AssumeRole"
  }]
}
```

**Required permissions:** CloudFormation, IAM, RDS, KMS, S3, EC2, Service Quotas

### Deploy the Application

```bash
sam build
sam deploy --guided --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM
```

**Parameters:**
- `EonAccountDomain` - Your Eon domain (e.g., `mycompany`)
- `EonProjectId` - Eon project ID (UUID)
- `EonAccountId` - Eon account ID
- `EonClientId` / `EonClientSecret` - API credentials
- `ManagementAccountId` - (Optional) For AWS Organizations
- `NotificationEmail` - (Optional) For completion alerts

## Usage

### Execute a Restore

**Via AWS Console:**
1. Go to Step Functions → `eon-bulk-recovery-workflow` → **Start Execution**

**Via AWS CLI:**
```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input file://execution-input.json
```

### Execution Input

**Minimal example (AWS Organizations):**
```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "restoreRegion": null,
  "snapshotDate": null,
  "dynamodbRegionalWcuLimit": 40000,
  "crossAccountRoleArn": null,
  "vpcConfigs": [{
    "region": "us-east-1",
    "vpc": "vpc-xxx",
    "subnetsPerAvailabilityZone": [
      {"availabilityZone": "us-east-1a", "subnetId": "subnet-xxx"},
      {"availabilityZone": "us-east-1b", "subnetId": "subnet-yyy"}
    ],
    "securityGroups": {
      "restoreServer": ["sg-xxx"],
      "restoredRdsInstance": ["sg-yyy"]
    }
  }]
}
```

**Parameter reference:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `sourceAccountId` | Yes | Account with backed-up resources |
| `restoreAccountId` | Yes | Target account for restored resources |
| `restoreRegion` | No | Force all resources to this region (null = use source regions) |
| `snapshotDate` | No | Date for snapshot selection (YYYY-MM-DD, null = latest) |
| `dynamodbRegionalWcuLimit` | No | DynamoDB WCU limit per region (default: 40000) |
| `vpcConfigs` | Yes | Network configuration per region (creates KMS keys and RDS subnet groups automatically) |
| `crossAccountRoleArn` | No | Custom role ARN or null for Organizations |
| `restoreAccountName` | No | Display name in Eon (null = auto-generate) |

## How It Works

1. **Bootstrap** - Creates KMS keys in each region, RDS subnet groups, IAM roles
2. **Connect** - Registers restore account with Eon
3. **Configure** - Sets up VPC connectivity
4. **List Snapshots** - Retrieves resources and their snapshots, extracts table sizes
5. **Initiate Restores** - Allocates WCUs per-region (38k per region = 95% of 40k, proportional to table sizes, 50 WCU default for zero-size tables)
6. **Monitor** - Polls until completion (default: 30 hours max)

![Notification example](./screenshot_output.png)

### Monitoring Timeout

Configure in `template.yaml`:
```yaml
MAX_MONITORING_ITERATIONS: '360'  # 360 × 5min = 30 hours
```

## Monitoring

- **Step Functions Console** - Visual workflow progress
- **CloudWatch Logs** - `/aws/lambda/eon-bulk-recovery-handler`
- **SNS Notifications** - Email alerts with job summaries