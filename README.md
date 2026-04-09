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

**Required permissions:** CloudFormation, IAM, RDS, KMS, S3, EC2, DynamoDB, Service Quotas

> **DynamoDB permissions** are needed for in-place restore WCU scaling (`recoveryStackNames`). The role must include: `dynamodb:DescribeTable`, `dynamodb:UpdateTable`, `dynamodb:TagResource`, `dynamodb:UntagResource`, `dynamodb:ListTagsOfResource`. Built-in admin roles (`AWSControlTowerExecution`, `OrganizationAccountAccessRole`) already have these.

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

> **Important:** All fields must be present in the execution input — Step Functions will fail at runtime if any key referenced in the state machine is missing. Use `null`, `[]`, or `false` for unused optional fields.

```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "restoreRegion": null,
  "snapshotDate": null,
  "resourceNamePrefix": null,
  "dynamodbRegionalWcuLimit": 40000,
  "crossAccountRoleArn": null,
  "excludeEC2TagKeys": [],
  "recoveryStackNames": [],
  "recoveryStacksOnly": false,
  "restoreAccountName": null,
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

**Tag exclusion** — add to the minimal example above:
```json
  "excludeEC2TagKeys": ["aws:autoscaling:groupName", "kubernetes.io/cluster/my-cluster"]
```
Tag keys matching this list will be filtered from restored EC2 instances and their volumes.

**Recovery stacks (in-place restore)** — add to the minimal example above:
```json
  "recoveryStackNames": ["OrdersServiceStack", "UserDataStack"]
```
The workflow queries the named CloudFormation stacks in the restore account for pre-created DynamoDB tables and S3 buckets. For DynamoDB, if a table matching the source table name AND source region is found, an **in-place restore** is performed instead of creating a new table — the table's WCU is temporarily scaled up (same allocation as new table restores) and restored to its original throughput after the restore completes. For S3, if a stack bucket has a tag (key = `s3InPlaceTagKey`, default `eon_functional_id`) matching the source bucket's tag value, an **in-place restore** is performed to that existing bucket.

**Stacks-only mode** — set both fields to only restore stack-matched resources:
```json
  "recoveryStackNames": ["OrdersServiceStack"],
  "recoveryStacksOnly": true
```
Skips EC2, RDS, and any DynamoDB/S3 resources that don't match a stack table or bucket.

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

**In-place restore:** When using `recoveryStackNames`, if a source S3 bucket has a tag matching `s3InPlaceTagKey` (default: `eon_functional_id`) and a recovery-stack bucket has the same tag value, the data is restored directly to the existing bucket. The tag value can be any string — the original bucket name, a hash, a UUID, etc. — it just needs to match between source and target. This avoids naming limitations and ensures the bucket name matches what the recovery stack's application code expects.

**Important:** Applications referencing S3 bucket names will need configuration updates after restore to point to the new bucket names. The restored bucket names are included in the workflow output and completion notification.

**Parameter reference:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `sourceAccountId` | Yes | Account with backed-up resources |
| `restoreAccountId` | Yes | Target account for restored resources |
| `restoreRegion` | No | Force all resources to this region (null = use source regions) |
| `snapshotDate` | No | Date for snapshot selection (YYYY-MM-DD, null = latest) |
| `resourceNamePrefix` | No | Prefix for restored resource names (null = use original names) |
| `dynamodbRegionalWcuLimit` | No | Total WCU budget per region across all tables (default: 40000). See [DynamoDB WCU Allocation](#dynamodb-wcu-allocation) |
| `dynamodbTableWcuMax` | No | Max WCU any single table can receive (default: 40000). Caps individual tables when the regional limit is raised |
| `vpcConfigs` | Yes | Network configuration per region (creates KMS keys and RDS subnet groups automatically) |
| `crossAccountRoleArn` | No | Custom role ARN or null for Organizations |
| `restoreAccountName` | No | Display name in Eon (null = auto-generate) |
| `excludeEC2TagKeys` | No | List of tag keys to exclude from restored EC2 instances and volumes (default: []) |
| `recoveryStackNames` | No | List of CloudFormation stack names to scan for pre-created DynamoDB tables and S3 buckets (default: []) |
| `recoveryStacksOnly` | No | When `true`, only restore resources that match a stack table/bucket — skip EC2, RDS, and unmatched DynamoDB/S3 (default: false) |
| `s3InPlaceTagKey` | No | Tag key used to match source and target S3 buckets for in-place restore (default: `"eon_functional_id"`). The tag value can be any string — bucket name, hash, UUID, etc. |

### DynamoDB WCU Allocation

When restoring DynamoDB tables, the workflow distributes Write Capacity Units (WCUs) across tables **per-region** to maximize restore throughput without exceeding account limits. This applies to both new table restores and in-place restores (recovery stack tables).

**How it works:**
1. Tables with known sizes (from Eon's resource inventory) are allocated first, proportionally to their size — a 10 GB table gets 10× the WCUs of a 1 GB table
2. 95% of the regional WCU limit is used (default: 38,000 out of 40,000) to leave headroom
3. Tables with unknown sizes (0 bytes) receive a small default allocation (50 WCU) from whatever capacity remains

**Example:** 3 tables in us-east-1 with a 40,000 WCU limit:

| Table | Size | Allocation |
|-------|------|------------|
| orders | 30 GB (75%) | 28,500 WCU |
| users | 10 GB (25%) | 9,500 WCU |
| cache | 0 GB (unknown) | 50 WCU |

**Per-table cap:** `dynamodbTableWcuMax` (default: 40,000) limits the WCU assigned to any single table. This matters when you raise the regional limit — e.g., with `dynamodbRegionalWcuLimit: 80000` and two tables, each table would get ~38,000 WCU rather than one table consuming 76,000. The restore process uses DynamoDB provisioned capacity, so the per-table cap prevents a single table from monopolizing throughput.

**Tuning:** The standard AWS account limit is 80,000 WCU per region. `dynamodbRegionalWcuLimit` defaults to 40,000 (half) to avoid consuming all capacity during restore — increase it if the restore account is dedicated or has headroom.

**In-place restore WCU scaling:** For recovery stack tables, the workflow:
1. Reads the table's current billing mode and throughput (including GSIs)
2. Temporarily switches to provisioned mode with the allocated WCU (or scales up existing provisioned WCU)
3. Sets **warm throughput** to pre-allocate DynamoDB partitions (see below)
4. Initiates the Eon restore
5. Restores the original billing mode and throughput after the restore job completes (or fails)

Original settings are preserved via DynamoDB tags (`eon:original_billing_mode`, `eon:original_wcu`, `eon:original_rcu`) as a safety mechanism. If WCU restoration fails, the completion notification includes an **ACTION REQUIRED** section listing affected tables and their original settings.

### DynamoDB Warm Throughput

DynamoDB partitions have a hard limit of **1,000 WCU each**. Even with 40,000 WCU provisioned on a table, if the table only has a few partitions, individual partitions will be throttled during bulk writes (`WriteKeyRangeThroughputThrottleEvents`). This is the primary cause of restore throttling for large tables.

**Warm throughput** tells DynamoDB to pre-allocate enough partitions to handle the specified write throughput immediately, rather than scaling reactively. The workflow automatically sets warm throughput on all restored DynamoDB tables:

- **In-place restores (recovery stack tables):** Warm throughput is set after WCU scaling, **before** the restore begins — partitions are pre-allocated before any writes start.
- **New table restores:** The monitoring handler sets warm throughput once the Eon-created table exists and is ACTIVE. Even mid-restore, this helps by pre-allocating more partitions for the remaining write volume.
- **GSIs:** Warm throughput is also applied to Global Secondary Indexes, since base table writes trigger GSI updates.

**Important characteristics:**
- Works with both **provisioned** and **on-demand** (PAY_PER_REQUEST) billing modes
- Warm throughput values **can only be increased, never decreased** — this is a permanent setting
- **One-time cost:** ~$0.00065/WCU in us-east-1 (e.g., warming from 4,000 to 40,000 WCU ≈ $23)
- Subject to account table-level write throughput quota (default: 40,000 WCU, can be increased via Service Quotas)
- Non-fatal: if warm throughput fails, the restore continues with standard throughput scaling

Warm throughput is enabled by default. To disable it, set `dynamodbWarmThroughput` to `false` in the execution input (the Lambda handler reads this directly — it is not passed through the state machine definition).

**Custom cross-account role permissions:** When using a custom `crossAccountRoleArn`, the role must include DynamoDB permissions for WCU scaling — see [Cross-Account Setup (Option B)](#option-b-manual-cross-account-role) for the full list. Built-in admin roles already have these.

## How It Works

1. **Bootstrap** - Creates KMS keys in each region, RDS subnet groups, IAM roles
2. **Connect** - Registers restore account with Eon
3. **Configure** - Sets up VPC connectivity
4. **List Snapshots** - Retrieves resources and their snapshots, extracts table sizes
5. **Initiate Restores** - If `recoveryStackNames` provided:
   - **DynamoDB**: Queries stacks for `AWS::DynamoDB::Table` resources, uses in-place restore for matches (by table name + region), temporarily scales up WCU
   - **S3**: Queries stacks for `AWS::S3::Bucket` resources, uses in-place restore for matches (by `s3InPlaceTagKey` tag, default `eon_functional_id`)
   - Otherwise: Creates new tables with allocated WCUs (38k per region = 95% of 40k) and new S3 buckets with hash suffixes
6. **Monitor** - Polls until completion (default: 30 hours max), restores DynamoDB WCU to original settings as each in-place restore completes

![Notification example](./screenshot_output.png)

### Lambda Timeout

The Lambda function has a 15-minute timeout (configured in `template.yaml`). Each restore job takes ~5 seconds to initiate (API call + rate-limit pause), so a single invocation can handle roughly **~150 resources**. If you are restoring hundreds of resources simultaneously, the function may time out before all jobs are initiated. This is a known limitation — a future enhancement will add batching/continuation support.

### Monitoring Timeout

Configure in `template.yaml`:
```yaml
MAX_MONITORING_ITERATIONS: '360'  # 360 × 5min = 30 hours
```

## Monitoring

- **Step Functions Console** - Visual workflow progress
- **CloudWatch Logs** - `/aws/lambda/eon-bulk-recovery-handler`
- **SNS Notifications** - Email alerts with job summaries