# lesson23-timeout-context-v1

Service payment-api, environment lab. This runbook applies to every teaching region.

The payments platform owner is the payment-oncall team. This is reference material, with no current incident observations.

Use the service and environment in the current task when querying live or frozen telemetry. Keep the region and incident identity attached to each receipt. A healthy past investigation does not prove that the present service is healthy.

The timeout-triage Skill chooses which investigation branch to execute. query_service_health reads the service health. query_upstream_latency reads the current upstream latency. query_pool_config describes the configured connection limit. query_pool_effective reports the currently effective limit and pool observations. A configuration difference is an observation that requires further investigation before assigning a root cause.

Report only checks that actually returned a receipt. Escalate unresolved findings to the payment-oncall team with the Session and report Item references. This read-only exercise performs no recovery action.
