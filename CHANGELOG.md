# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-31

### Added

- **`resourceTypes` execution input.** Restricts a run to a subset of `AWS_EC2`, `AWS_RDS`,
  `AWS_S3`, `AWS_DYNAMO_DB`. Omitted or `[]` means all of them. An unrecognised type fails the
  run at the `List Resources` step instead of restoring nothing.
- **`resourceIds` execution input.** Restricts a run to specific resources. Accepts Eon resource
  IDs (UUIDs), cloud provider resource IDs (`i-…`, bucket or table names), or a mix. IDs that
  match nothing are logged as a warning so a typo is distinguishable from a resource with no
  backups.
- **`snapshotDate: "latest"`.** Explicit sentinel for "each resource's most recent snapshot",
  equivalent to `null`. A malformed date now fails at the input rather than partway through
  snapshot selection.
- **Reporting for resources with nothing to restore.** `Get Snapshots` records every resource it
  skips and why, including the resource's latest available snapshot time when a pinned date
  misses. The reasons cover a missing snapshot, a snapshot with no EC2 properties or no volumes,
  and a failed lookup.
- **`No Snapshots Found` terminal state.** When resources are in scope but none of them have a
  snapshot for the requested date, the workflow sends a `NO SNAPSHOTS FOUND` notification listing
  the affected resources and stops, instead of initiating zero jobs and reporting success.
- **`AWS::DynamoDB::GlobalTable` in recovery-stack discovery.** CDK's `TableV2` construct emits
  this resource type, and matching only `AWS::DynamoDB::Table` silently missed those tables, so
  they were restored as new tables or skipped under `recoveryStacksOnly`. Replica regions are read
  from `DescribeTable`, so a global table matches a source resource in any of its regions and is
  restored into the stack's own region.
- MPL-2.0 license.

### Changed

- **Monitoring ceiling raised from 30 to 60 hours** (`MAX_MONITORING_ITERATIONS` 360 → 720).
- **Monitoring timeout now terminates the workflow.** On reaching the ceiling with jobs still
  running, the monitor sends one `TIMEOUT` notification and the state machine ends in
  `Monitoring Timed Out`. It previously kept polling and re-sending the timeout notification every
  five minutes.
- **Completion notification carries skipped resources.** Adds a `No Snapshot Available` count to
  the job summary and a `Resources Without a Snapshot (not restored)` section with a reason per
  resource. A run where every job succeeded but some resources were skipped now reports
  `PARTIAL SUCCESS` rather than `SUCCESS`.
- **Resource type filtering moved server-side.** `resourceType` is applied by the Eon API rather
  than after pagination, which keeps the Step Functions payload under its 256 KB limit on large
  accounts.

### Fixed

- **Resource ID filters are routed by what the API accepts, not by the shape of the ID.** DynamoDB
  resources have a UUID as their `providerResourceId`, so a UUID cannot be assumed to be an Eon
  resource ID. The `providerResourceId` filter matches any string; the `id` filter is parsed as a
  UUID server-side and rejects anything else. Every supplied ID is now queried as a provider ID,
  UUID-shaped ones are additionally queried as Eon IDs, and the results are unioned.

## [1.0.0] - 2026-07-10

First tagged baseline. Covers the workflow as it stood before the changes above: bootstrap of the
restore account (IAM, per-region KMS keys, RDS subnet groups), account connection and VPC
configuration in Eon, resource and snapshot enumeration, restore initiation for EC2, RDS, S3 and
DynamoDB across regions, and monitoring through to a completion notification.

Also includes the hardening released on this date:

- **Restore role existence is verified before use.** If the IAM stack exists but its restore role
  is missing (deleted out of band, or a prior create rolled back), the workflow stops with an
  actionable error rather than handing Eon a role ARN it cannot assume.
- **Connect retries against newly installed roles.** A restore account already registered as
  `DISCONNECTED` or `INSUFFICIENT_PERMISSIONS` is reconnected and polled for `CONNECTED`, giving
  roles installed moments earlier by bootstrap time to propagate.
- `JOB_REJECTED` and `JOB_SKIPPED` are handled as terminal job statuses.

[1.1.0]: https://github.com/eon-solutions/eon-bulk-recovery/releases/tag/v1.1.0
[1.0.0]: https://github.com/eon-solutions/eon-bulk-recovery/releases/tag/v1.0.0
