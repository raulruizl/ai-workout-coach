# Build Roadmap — Dependency DAG

Reference for what can be built in parallel vs what's blocked. See `CLAUDE.md` for the
architecture these nodes implement. Terraform state is local (no remote backend).

## Stages

Each stage is a wavefront: everything in it can be built in parallel once the previous
stage's nodes exist.

### Stage 0 — Bootstrap
- **A1** TF bootstrap (providers, local state) — root, no deps

### Stage 1 — Foundation resources (parallel, all depend only on A1)
- **A2** S3 buckets: bronze / silver / gold, partitioned by `user_id`
- **A3** SSM parameter: SecureString placeholder for Hevy API key
- **A5** DynamoDB table: sole serving layer, `LATEST` + `WEEK#<date>` items per user
- **A6** SQS DLQ + SNS topic: pipeline failure alerting

### Stage 2 — Hardening + pipeline compute (parallel, fixture-tested)
- **A7** S3 hardening: Block Public Access, SSE, bronze versioning — depends: A2
- **B1** Extract Lambda: Hevy API → bronze, incremental via `/v1/workouts/events` — depends: A2, A3
- **B2** Glue job bronze→silver: flatten to set-grain parquet — depends: A2
- **B3** Glue job silver→gold: weekly aggregates, warmup sets excluded — depends: A2
- **B4** Sync Lambda: gold → DynamoDB (`LATEST` + `WEEK#<date>`) — depends: A2, A5

Real B1→B2→B3→B4 run-order only matters at orchestration time (Stage 3), not dev time.

### Stage 3 — Orchestration convergence + agent scaffold
- **D1** Step Functions state machine (Catch/Retry → SQS DLQ) — depends: B1, B2, B3, B4, A6
- **F1** Bedrock AgentCore + Strands scaffold, system prompt — depends: A5 (seeded with fixtures)

D1 is the one place parallel Stage-2 work must rejoin. F1 doesn't need D1/D2 running — only
needs A5 to exist with fixture data.

### Stage 4 — Schedule + agent tools
- **D2** EventBridge Scheduler rule (daily cron) — depends: D1
- **F2** Agent tools + tests: `get_latest_stats`, `query_workout_history`, `compute_volume_trend`,
  `estimate_1rm`, `detect_stall` — all DynamoDB-only — depends: F1

### Stage 5 — Alarms + chat bridge
- **D3** CloudWatch alarms on state machine — depends: D1, A6
- **G1** API Gateway WebSocket + chat-bridge Lambda → Bedrock AgentCore — depends: F1, F2

### Stage 6 — UI
- **G2** Amplify Hosting + React SPA (chat + live dashboard) — depends: G1 (`chat_token` /
  `dashboard_update` message contract)

### Cross-cutting — H (every node)
Least-privilege IAM scoped exactly to that resource before it counts as "done" (per
security-expert findings in CLAUDE.md). Not a separate stage — a gate on each node.

## Recommended first slice

Stage 0 + Stage 1 + A7 from Stage 2 — `A1, A2, A3, A5, A6, A7`. Everything else depends on
this, it has no internal blocking, and it's verifiable with `terraform plan`/`validate` before
any Lambda/Glue code is written.

## Serving layer note

Glue Data Catalog and Athena are **not** part of this architecture — the app serves data
through the UI only, no ad-hoc SQL layer. DynamoDB (A5) is the sole serving layer for the
agent, holding both `LATEST` and per-week historical items. Gold S3 parquet still exists for
lineage/archival (medallion model), but only B4 reads it.
