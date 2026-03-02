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
  "excludeEC2TagKeys": [],
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

**Example with tag exclusion:**
```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "excludeEC2TagKeys": ["aws:autoscaling:groupName", "kubernetes.io/cluster/my-cluster"],
  "vpcConfigs": [...]
}
```
Note: Tag keys matching `excludeEC2TagKeys` will be filtered from restored EC2 instances and their volumes.

**Example with pre-created DynamoDB tables (CDK/CloudFormation stacks):**
```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "recoveryStackNames": ["OrdersServiceStack", "UserDataStack"],
  "vpcConfigs": [...]
}
```
Note: When `recoveryStackNames` is provided, the workflow will query the specified CloudFormation stacks in the restore account to discover DynamoDB tables and S3 buckets created by those stacks. For DynamoDB, if a table matching the source table name AND source region is found, an **in-place restore** is performed to that existing table instead of creating a new table. For S3, if a stack bucket has an `eon_functional_id` tag whose value matches the same tag on the source bucket (from the Eon snapshot's original tags), an **in-place restore** is performed to that existing bucket instead of creating a new one.

**Example with custom resource name prefix:**
```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "resourceNamePrefix": "dr-",
  "vpcConfigs": [...]
}
```
Note: By default, resources are restored with their **original names** (designed for full account recovery to a new account). Set `resourceNamePrefix` to add a prefix (e.g., "dr-mydb" instead of "mydb").

### S3 Bucket Naming Limitations

S3 bucket names are **globally unique** across all AWS accounts worldwide. This means:

- **Original bucket names cannot be reused** - The source bucket still exists, so the same name is unavailable
- **A hash suffix is always added** - Restored buckets are named `{original-name}-{hash}` (e.g., `my-bucket-a1b2c3d4`)
- **The `resourceNamePrefix` still applies** - With a prefix, buckets become `{prefix}{original-name}-{hash}`
- **Long bucket names are truncated** - S3 limits names to 63 characters; when the combined name (original + hash suffix) exceeds 63 characters, the original name is truncated to preserve the hash suffix
- **AWS system tags are filtered** - Tags with reserved prefixes (`aws:`, `elasticbeanstalk:`) from the original bucket are automatically excluded as they cannot be manually recreated

**In-place restore:** When using `recoveryStackNames`, if a source S3 bucket has an `eon_functional_id` tag and a matching bucket exists in the recovery stack with the same tag value, the data is restored directly to the existing bucket without creating a new one. This avoids naming limitations and ensures the bucket name matches what the recovery stack's application code expects.

**Important:** Applications referencing S3 bucket names will need configuration updates after restore to point to the new bucket names. The restored bucket names are included in the workflow output and completion notification.

**Parameter reference:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `sourceAccountId` | Yes | Account with backed-up resources |
| `restoreAccountId` | Yes | Target account for restored resources |
| `restoreRegion` | No | Force all resources to this region (null = use source regions) |
| `snapshotDate` | No | Date for snapshot selection (YYYY-MM-DD, null = latest) |
| `resourceNamePrefix` | No | Prefix for restored resource names (null = use original names) |
| `dynamodbRegionalWcuLimit` | No | DynamoDB WCU limit per region (default: 40000) |
| `vpcConfigs` | Yes | Network configuration per region (creates KMS keys and RDS subnet groups automatically) |
| `crossAccountRoleArn` | No | Custom role ARN or null for Organizations |
| `restoreAccountName` | No | Display name in Eon (null = auto-generate) |
| `excludeEC2TagKeys` | No | List of tag keys to exclude from restored EC2 instances and volumes (default: []) |
| `recoveryStackNames` | No | List of CloudFormation stack names to scan for pre-created DynamoDB tables and S3 buckets (default: []) |

## How It Works

1. **Bootstrap** - Creates KMS keys in each region, RDS subnet groups, IAM roles
2. **Connect** - Registers restore account with Eon
3. **Configure** - Sets up VPC connectivity
4. **List Snapshots** - Retrieves resources and their snapshots, extracts table sizes
5. **Initiate Restores** - If `recoveryStackNames` provided:
   - **DynamoDB**: Queries stacks for `AWS::DynamoDB::Table` resources, uses in-place restore for matches (by table name + region)
   - **S3**: Queries stacks for `AWS::S3::Bucket` resources, uses in-place restore for matches (by `eon_functional_id` tag)
   - Otherwise: Creates new tables with allocated WCUs (38k per region = 95% of 40k) and new S3 buckets with hash suffixes
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