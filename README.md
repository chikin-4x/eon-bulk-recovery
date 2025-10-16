# Eon Bulk Recovery Application

Automated AWS application for performing bulk recovery of cloud resources from Eon backups. This application orchestrates the complete recovery workflow from bootstrapping a restore account to monitoring restore job completion.

## Overview
![Example workflow in AWS Step Functions](./screenshot_statemachine.png)

This application uses AWS Step Functions to orchestrate a multi-step workflow that:

1. **Bootstraps the restore account** - Deploys IAM permissions, creates RDS subnet groups, and creates KMS encryption keys
2. **Connects the restore account to Eon** - Registers the restore account with Eon via REST API
3. **Configures VPC connectivity** - Sets up networking for the Eon restore process
4. **Lists protected resources** - Retrieves all backed-up resources from the source account
5. **Retrieves snapshots** - Gets snapshot IDs for all resources to restore
6. **Initiates restore jobs** - Starts restore operations for all snapshots
7. **Monitors jobs** - Polls job status until all restores complete
8. **Sends notifications** - Publishes completion status to SNS

![Example bulk restore notification](./screenshot_output.png)

## Supported Resource Types

- **AWS EC2** - EC2 instances with EBS volumes
- **AWS RDS** - RDS database instances
- **AWS S3** - S3 buckets with objects
- **AWS DynamoDB** - DynamoDB tables

## Prerequisites

### 1. Eon Account Setup

- Active Eon account with API access
- API credentials (Client ID and Secret) with appropriate permissions
- Project ID from your Eon account
- Eon Account ID for IAM role external ID

### 2. AWS Setup

#### Workstation 
- AWS CLI configured
- AWS SAM CLI installed (`pip install aws-sam-cli`)
- Appropriate IAM permissions to deploy CloudFormation stacks

#### Restore Account (where resources will be restored)
- AWS account where restored resources will be created
- Must have a cross-account IAM role (see below)

### 3. Cross-Account Access

The application needs to deploy resources in the restore account. If you're using **AWS Organizations** but want to deploy this application in a **different account** (not the management account), you can use role chaining:

**Step 1: Deploy the main application with ManagementAccountId**

First, deploy the main application in your backup account with the `ManagementAccountId` parameter:

```bash
sam build
sam deploy --guided
# When prompted for ManagementAccountId, enter your AWS Organizations management account ID
```

Or update the parameter in your `samconfig.toml`:

```toml
[default.deploy.parameters]
parameter_overrides = "ManagementAccountId=111111111111 ..."
```

This creates the Lambda execution role that will be referenced in the next step.

**Step 2: Deploy the chaining role in the Management Account**

Now that the Lambda execution role exists, deploy the chaining role in your **AWS Organizations Management Account**:

```bash
cd eon-bulk-recovery
aws cloudformation deploy \
  --template-file management-account-role.yaml \
  --stack-name eon-bulk-recovery-chain-role \
  --parameter-overrides \
      BackupAccountId=<BACKUP_ACCOUNT_ID> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Replace `<BACKUP_ACCOUNT_ID>` with the AWS account ID where you deployed the Eon Bulk Recovery application (from Step 1).

This creates an `EonBulkRecoveryChainRole` that:
- Trusts the `EonBulkRecoveryLambdaRole` from your backup account
- Can assume either `AWSControlTowerExecution` (for Control Tower accounts) or `OrganizationAccountAccessRole` (for standard Organizations accounts) in any organization member account

**How it works:**

The application uses a two-step role chaining process:
1. Lambda (in backup account) → Assumes `EonBulkRecoveryChainRole` (in management account)
2. Chain role → Assumes `AWSControlTowerExecution` or `OrganizationAccountAccessRole` (in restore account)

The application automatically detects whether the restore account is managed by Control Tower or standard AWS Organizations and uses the appropriate role.

This allows you to centralize the Eon Bulk Recovery application in a dedicated backup/recovery account while maintaining secure access to all organization member accounts.

#### Alternative: Manual Cross-Account Role (For Non-Organization Scenarios)

If the restore account is **not** in your AWS Organization, or you want to use **least-privilege permissions**, create a manual cross-account role:

**Step 1: Create the IAM role**

In the restore account's IAM console, create a new role with the following trust policy (replace `<MANAGEMENT_ACCOUNT_ID>` with your management account ID):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<MANAGEMENT_ACCOUNT_ID>:role/EonBulkRecoveryLambdaRole"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

**Step 2: Attach permissions policy**

Attach the following IAM policy to the role (copy-paste ready):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormationPermissions",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate",
        "cloudformation:UpdateStack",
        "cloudformation:ListStacks"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMPermissions",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile",
        "iam:TagRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RDSPermissions",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBSubnetGroup",
        "rds:DeleteDBSubnetGroup",
        "rds:DescribeDBSubnetGroups",
        "rds:AddTagsToResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "KMSPermissions",
      "Effect": "Allow",
      "Action": [
        "kms:CreateKey",
        "kms:CreateAlias",
        "kms:DeleteAlias",
        "kms:DescribeKey",
        "kms:GetKeyPolicy",
        "kms:PutKeyPolicy",
        "kms:TagResource",
        "kms:ScheduleKeyDeletion"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3Permissions",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketEncryption",
        "s3:PutBucketTagging",
        "s3:PutBucketVersioning",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketLocation"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2Permissions",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeImages",
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeAvailabilityZones"
      ],
      "Resource": "*"
    }
  ]
}
```

**Note:** This policy provides the minimum permissions needed for the bulk recovery workflow to function. For production environments, consider further restricting resource-level permissions based on your specific requirements.

## Deployment

### 1. Clone and prepare the repository

```bash
cd eon-bulk-recovery
```

### 2. Build and deploy using AWS SAM

```bash
sam build
sam deploy --guided
```

### 3. Provide deployment parameters

You will be prompted for:
- **Stack Name**: Name for the CloudFormation stack (e.g., `eon-bulk-recovery`)
- **EonAccountDomain**: Your Eon account domain (e.g., `mycompany` for mycompany.console.eon.io)
- **EonProjectId**: Your Eon project ID (UUID format)
- **EonAccountId**: Eon account ID for IAM external ID
- **EonClientId**: Eon API client ID
- **EonClientSecret**: Eon API client secret
- **ManagementAccountId**: (Optional) AWS Organizations management account ID - only required if using role chaining (see Cross-Account Access section)
- **NotificationEmail**: (Optional) Email for SNS notifications

### 4. Confirm the email subscription

If you provided a notification email, check your inbox and confirm the SNS subscription.

## Usage

### Starting a Bulk Recovery

Execute the Step Functions state machine with the following input.

**Note:** The `ManagementAccountId` is configured at deployment time as a CloudFormation parameter (not in the execution input). The `crossAccountRoleArn` is a runtime parameter that must be included in the execution input - set it to `null` for AWS Organizations/role chaining, or provide a specific role ARN for manual cross-account scenarios.

**For AWS Organizations or role chaining (set crossAccountRoleArn to null):**

```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "restoreAccountName": null,
  "restoreRegion": "us-east-1",
  "snapshotDate": "2024-01-15",
  "crossAccountRoleArn": null,
  "vpcConfigs": [
    {
      "region": "us-east-1",
      "vpc": "vpc-0123456789abcdef0",
      "subnetsPerAvailabilityZone": [
        {
          "availabilityZone": "us-east-1a",
          "subnetId": "subnet-0123456789abcdef0"
        },
        {
          "availabilityZone": "us-east-1b",
          "subnetId": "subnet-0123456789abcdef1"
        },
        {
          "availabilityZone": "us-east-1c",
          "subnetId": "subnet-0123456789abcdef2"
        }
      ],
      "securityGroups": {
        "restoreServer": ["sg-0123456789abcdef0"],
        "restoredRdsInstance": ["sg-0123456789abcdef1"]
      }
    }
  ]
}
```

**For manual cross-account role (provide a specific crossAccountRoleArn):**

```json
{
  "sourceAccountId": "333333333333",
  "restoreAccountId": "222222222222",
  "restoreAccountName": null,
  "restoreRegion": "us-east-1",
  "snapshotDate": "2024-01-15",
  "vpcConfigs": [...],
  "crossAccountRoleArn": "arn:aws:iam::222222222222:role/EonBulkRecoveryCrossAccountRole"
}
```

### Parameter Descriptions

| Parameter | Required | Description |
|-----------|----------|-------------|
| `sourceAccountId` | Yes | AWS account ID containing the backed-up resources |
| `restoreAccountId` | Yes | AWS account ID where resources will be restored |
| `restoreAccountName` | No | Display name for the restore account in Eon. Set to `null` to auto-generate as `bulk-recovery-{restoreAccountId}` |
| `restoreRegion` | Yes | Primary AWS region for restores (default: us-east-1) |
| `snapshotDate` | No | Specific date for snapshot selection (YYYY-MM-DD). If omitted, uses latest snapshots |
| `vpcConfigs` | Yes | VPC connectivity configuration for Eon restore servers. Also used to create RDS subnet groups. Must include subnets for the restore region if restoring RDS instances. |
| `crossAccountRoleArn` | Yes | ARN of a custom cross-account role for manual scenarios, or `null` for AWS Organizations/role chaining. When `null`, automatically uses OrganizationAccountAccessRole or EonBulkRecoveryChainRole depending on deployment configuration. |

### Via AWS Console

1. Navigate to **Step Functions** in the AWS Console
2. Select the `eon-bulk-recovery-workflow` state machine
3. Click **Start Execution**
4. Paste the JSON input
5. Click **Start Execution**

### Via AWS CLI

```bash
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:eon-bulk-recovery-workflow \
  --input file://execution-input.json
```

## Monitoring

### Step Functions Console

Monitor the execution progress in the AWS Step Functions console:
- View the visual workflow
- See current step and status
- Review execution history and logs

### CloudWatch Logs

Lambda function logs are available in CloudWatch Logs:
- Log group: `/aws/lambda/eon-bulk-recovery-handler`
- Filter by execution ID or resource name

### SNS Notifications

Receive email notifications when the bulk recovery completes with:
- Summary statistics (total, completed, failed)
- Individual job statuses
- Job IDs for tracking in Eon

## Architecture Details

### Lambda Handlers

The application uses a single Lambda function with multiple handlers:

- **bootstrap.py** - Bootstrap restore account infrastructure
- **connect_account.py** - Connect restore account to Eon
- **configure_vpc.py** - Configure VPC connectivity
- **list_resources.py** - List protected resources
- **get_snapshots.py** - Retrieve snapshot IDs
- **initiate_restores.py** - Start restore jobs
- **monitor_jobs.py** - Monitor job status and send notifications

### Error Handling

The Step Functions workflow includes:
- **Automatic retries** - Each step retries up to 3 times with exponential backoff
- **Error catching** - Failures are caught and reported clearly
- **Timeout protection** - Maximum monitoring iterations prevent infinite loops

### Job Monitoring

- Jobs are polled every **5 minutes**
- Maximum monitoring time: **30 hours** (360 iterations)
- SNS notification sent when all jobs complete or timeout occurs

## Customization

### Instance Types

EC2 and RDS restore operations automatically mirror the source resource's instance type. If the source instance type cannot be determined, the following defaults are used:

```python
# EC2 default fallback
"instanceType": "t3.medium"

# RDS default fallback
"dbInstanceClass": "db.t3.micro"
```

You can modify these defaults in `src/handlers/initiate_restores.py` if needed.

### DynamoDB Write Capacity

Default write capacity for DynamoDB table restores is set to 40000 units. You can modify this in `src/handlers/initiate_restores.py`:

```python
"writeCapacityUnits": 40000
```

### Monitoring Timeout

Adjust the maximum monitoring duration via environment variable in `template.yaml`:

```yaml
Environment:
  Variables:
    MAX_MONITORING_ITERATIONS: '360'  # 360 * 5min = 30 hours
```