"""Lambda handler for monitoring restore jobs and sending SNS notifications."""

import os
import sys
import json
from typing import Dict, Any, List
import boto3

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Monitor restore job status and send SNS notification when all jobs complete.

    Input event:
        restoreJobs: List of restore jobs with job IDs
        iteration: Current iteration count (for tracking polling attempts)

    Returns:
        allComplete: Boolean indicating if all jobs are complete
        jobStatuses: Current status of all jobs
        completedJobs: Count of completed jobs
        failedJobs: Count of failed jobs
        runningJobs: Count of still-running jobs
        iteration: Incremented iteration count
    """
    restore_jobs = event["restoreJobs"]
    iteration = event.get("iteration", 0)
    max_iterations = int(os.environ.get("MAX_MONITORING_ITERATIONS", "360"))  # 360 * 5min = 30 hours

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    print(f"Monitoring {len(restore_jobs)} restore jobs (iteration {iteration})")

    # Track job statuses
    completed_count = 0
    failed_count = 0
    running_count = 0
    partial_count = 0

    job_statuses = []

    for job in restore_jobs:
        job_id = job.get("jobId")

        # Skip jobs that failed to initiate
        if not job_id:
            failed_count += 1
            job_statuses.append({
                **job,
                "currentStatus": "FAILED_TO_INITIATE"
            })
            continue

        try:
            # Get job status from Eon API
            response = eon_client.get_restore_job(job_id)
            job_details = response.get("job", {})
            job_execution = job_details.get("jobExecutionDetails", {})

            status = job_execution.get("status", "UNKNOWN")
            status_message = job_execution.get("statusMessage", "")

            job_status_record = {
                "jobId": job_id,
                "resourceName": job.get("resourceName"),
                "resourceType": job.get("resourceType"),
                "currentStatus": status,
                "statusMessage": status_message,
                "startTime": job_execution.get("startTime"),
                "endTime": job_execution.get("endTime"),
                "durationSeconds": job_execution.get("durationSeconds")
            }

            # Categorize by status
            if status == "JOB_COMPLETED":
                completed_count += 1
            elif status in ["JOB_FAILED", "JOB_CANCELED"]:
                failed_count += 1
            elif status == "JOB_PARTIAL":
                partial_count += 1
                # Treat partial as a type of completion for decision purposes
                completed_count += 1
            elif status in ["JOB_PENDING", "JOB_RUNNING"]:
                running_count += 1
            else:
                # Unknown status, treat as running
                running_count += 1

            job_statuses.append(job_status_record)

            print(f"Job {job_id} ({job.get('resourceName')}): {status}")

        except Exception as e:
            print(f"ERROR: Failed to get status for job {job_id}: {str(e)}")
            # Treat as still running, will check again next iteration
            running_count += 1
            job_statuses.append({
                "jobId": job_id,
                "resourceName": job.get("resourceName"),
                "resourceType": job.get("resourceType"),
                "currentStatus": "ERROR_CHECKING_STATUS",
                "statusMessage": str(e)
            })

    all_complete = (running_count == 0)

    print(f"Job summary: {completed_count} completed, {failed_count} failed, {partial_count} partial, {running_count} still running")

    result = {
        "restoreJobs": restore_jobs,  # Pass through for next iteration
        "allComplete": all_complete,
        "jobStatuses": job_statuses,
        "completedJobs": completed_count,
        "failedJobs": failed_count,
        "partialJobs": partial_count,
        "runningJobs": running_count,
        "totalJobs": len(restore_jobs),
        "iteration": iteration + 1
    }

    # If all jobs are complete or max iterations reached, send SNS notification
    if all_complete or iteration >= max_iterations:
        send_completion_notification(result, iteration >= max_iterations)

    return result


def send_completion_notification(job_summary: Dict[str, Any], timeout: bool) -> None:
    """
    Send SNS notification about bulk recovery completion.

    Args:
        job_summary: Summary of all job statuses
        timeout: Whether the monitoring timed out
    """
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")

    if not sns_topic_arn:
        print("No SNS topic ARN configured, skipping notification")
        return

    sns_client = boto3.client("sns")

    total_jobs = job_summary["totalJobs"]
    completed_jobs = job_summary["completedJobs"]
    failed_jobs = job_summary["failedJobs"]
    partial_jobs = job_summary["partialJobs"]
    running_jobs = job_summary["runningJobs"]

    if timeout:
        subject = "Eon Bulk Recovery - TIMEOUT"
        status_summary = f"⚠️ Monitoring timed out after {job_summary['iteration']} iterations"
    elif failed_jobs == 0 and partial_jobs == 0:
        subject = "Eon Bulk Recovery - SUCCESS"
        status_summary = f"✅ All {total_jobs} restore jobs completed successfully"
    elif completed_jobs > 0:
        subject = "Eon Bulk Recovery - PARTIAL SUCCESS"
        status_summary = f"⚠️ {completed_jobs}/{total_jobs} jobs completed ({failed_jobs} failed, {partial_jobs} partial)"
    else:
        subject = "Eon Bulk Recovery - FAILURE"
        status_summary = f"❌ All {total_jobs} restore jobs failed"

    # Build detailed message
    message_lines = [
        "Eon Bulk Recovery Status Report",
        "=" * 50,
        "",
        status_summary,
        "",
        f"Total Jobs: {total_jobs}",
        f"Completed: {completed_jobs}",
        f"Failed: {failed_jobs}",
        f"Partial: {partial_jobs}",
        f"Still Running: {running_jobs}",
        "",
        "Job Details:",
        "-" * 50
    ]

    # Add individual job statuses
    for job_status in job_summary["jobStatuses"]:
        resource_name = job_status.get("resourceName", "Unknown")
        resource_type = job_status.get("resourceType", "Unknown")
        status = job_status.get("currentStatus", "Unknown")
        job_id = job_status.get("jobId", "N/A")

        status_emoji = {
            "JOB_COMPLETED": "✅",
            "JOB_FAILED": "❌",
            "JOB_PARTIAL": "⚠️",
            "JOB_RUNNING": "🔄",
            "JOB_PENDING": "⏳",
            "JOB_CANCELED": "🚫"
        }.get(status, "❓")

        message_lines.append(f"{status_emoji} {resource_name} ({resource_type})")
        message_lines.append(f"   Status: {status}")
        message_lines.append(f"   Job ID: {job_id}")

        if job_status.get("statusMessage"):
            message_lines.append(f"   Message: {job_status['statusMessage']}")

        if job_status.get("durationSeconds"):
            duration_min = job_status["durationSeconds"] // 60
            message_lines.append(f"   Duration: {duration_min} minutes")

        message_lines.append("")

    message = "\n".join(message_lines)

    # Send SNS notification
    try:
        sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        print(f"Sent SNS notification to {sns_topic_arn}")
    except Exception as e:
        print(f"ERROR: Failed to send SNS notification: {str(e)}")
