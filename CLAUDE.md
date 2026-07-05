# Workout Coach — AI-Driven Fitness Coaching (Data Engineering Portfolio)

AI-driven workout coach. Pulls training data from Hevy API, pipelines it through a fully-AWS
serverless stack, and serves a chat + live dashboard UI backed by a Bedrock agent. Built as a
data engineering portfolio project — architecture choices favor demonstrating AWS-native
serverless patterns over minimizing engineering effort.

## Constraints (do not violate without re-running the relevant skill)

- **Budget**: ~$5–10/mo. This ruled out MWAA (min ~$200+/mo) and always-on Redshift.
- **Fully AWS**: no local Docker Compose components in the running system. Local dev/testing
  containers are fine; the deployed system is AWS-native only.
- **No Airflow.** Replaced by EventBridge Scheduler + Step Functions (see ADR-001). Don't
  reintroduce Airflow without revisiting that decision with the user.
- **Multi-user schema design, single-user tested.** Partition/model for multiple users even
  though only one real account exists today.
- **Single-user, no auth** on the UI for now (demo/portfolio stage). Flag before adding Cognito.

## Architecture

```
Hevy API (Pro tier, API key auth)
   │
EventBridge Scheduler
   ▼
Step Functions state machine
   ├─ Lambda: extract (incremental via GET /v1/workouts/events)
   │     → S3 bronze (raw JSON, append-only, partitioned user_id/ingest_date)
   ├─ Glue Python Shell: bronze → silver (flatten to set-grain parquet, typed, deduped)
   ├─ Glue Python Shell: silver → gold (weekly aggregates: volume, est_1RM, stall flags)
   └─ Lambda: sync gold → DynamoDB (LATEST + WEEK#<date> items per user)
   Catch/Retry per state → SQS DLQ → CloudWatch/SNS alert

Serving:
   DynamoDB ← sole serving layer, low-latency point lookups for agent tools
   (no Glue Catalog, no Athena — app serves data through the UI only, no ad-hoc SQL layer)

AI Coach (decoupled from pipeline — separate consumer):
   Bedrock AgentCore (Strands SDK), Claude as brain
   Deterministic tools (pure math/data-fetch, no domain judgment in code):
     get_latest_stats, query_workout_history, compute_volume_trend,
     estimate_1rm (Epley, reps ≤ 12 only), detect_stall
   Fitness domain reasoning lives in the agent SYSTEM PROMPT, not tool code
   (user has no fitness domain expertise — that knowledge is prompt-engineered, not hardcoded)

UI:
   React SPA on AWS Amplify Hosting
   API Gateway WebSocket + Lambda bridge → Bedrock AgentCore
   Chat responses stream as "chat_token"; tool-call results push as "dashboard_update"
   → dashboard tiles update live, mid-conversation, driven by the same tool calls as the chat
```

## Data model

**Bronze** (`s3://.../bronze/workouts/user_id=<id>/ingest_date=<yyyy-mm-dd>/`) — raw Hevy JSON,
gzip, append-only, immutable. Never overwritten, never has rows dropped.

**Silver** (`silver.sets`, parquet, partitioned `user_id`/`year_month`) — one row per **set**
(finest grain). Keeps ALL sets including warmups (`is_warmup` flag, not filtered) — business
rules apply downstream, not here.

**Gold** (`gold.weekly_exercise_stats`, `gold.weekly_summary`, partitioned `user_id`/`week`) —
aggregated. This is where `is_warmup=true` sets get excluded from volume/1RM calculations.

**DynamoDB** (`workout_coach_stats`, PK `user_id`, SK `stat_type`) — sole serving layer for the
agent, derived cache, not source of truth. `SK=LATEST` single-item-per-user for fast reads, plus
`SK=WEEK#<date>` items so `query_workout_history`/trend tools have history to read without a
query engine.

Full lineage: `Hevy API → bronze (raw) → silver.sets (flattened, nothing dropped) → gold (business rules applied) → DynamoDB (agent cache — LATEST + WEEK#<date> items)`.

## Security controls (from security-expert review — apply to every component)

- **Per-component least-privilege IAM.** No shared "do everything" role. Each Lambda/Glue job/
  agent tool role gets only the specific actions + resource ARNs it needs (see VULN-002 in
  project memory / conversation history for the full per-component breakdown).
- **Secrets**: Hevy API key in SSM Parameter Store SecureString (not Secrets Manager — no
  rotation need since Hevy has no key-rotation API; not a downgrade). Never in Lambda env vars,
  never in Step Functions state input/payload — fetch inside the Lambda at invoke time.
- **S3**: Block Public Access enabled account-wide. SSE encryption on all buckets. Versioning on
  bronze.
- **Agent tool inputs**: never string-interpolate user/agent-supplied values into DynamoDB key
  construction. Strict validation (UUID/known-value checks) before any key/query build.
- **Agent tools are read-only.** No destructive/write tools exist today. If one is ever added,
  stop and add human-in-the-loop confirmation before shipping it — don't skip this gate.
- **Data classification**: user_id/workout data = Internal. body_measurements = Confidential
  (treat with same encryption, don't expose raw on any public surface). Hevy API key =
  Restricted. No GDPR/HIPAA trigger at this scale, but encrypt-at-rest + least-privilege apply
  regardless.

## Testing (non-negotiable)

**Every development step ships with unit tests in the same change — not deferred.** Applies to
every Lambda, every Glue Python Shell job, every agent tool function. Tests must cover the data
quality rules above: warmup-set exclusion in Gold, null handling for time/distance-based
exercises (no `weight_kg`/`reps`), dedup logic on `(workout_id, exercise_template_id, set_index)`,
and the `estimate_1rm` reps>12 edge case (formula unreliable above that threshold — return null,
don't compute).

## Decision record (ADRs)

- **ADR-001**: Step Functions + EventBridge over Airflow (any hosting). Zero idle cost, no host
  to manage, native retry/DLQ per state. Semi-one-way once Lambdas/Glue jobs are built as state
  machine tasks — don't relitigate lightly.
- **ADR-002**: S3 (Bronze/Silver/Gold) over Redshift. Serverless storage, no idle compute cost;
  partition scheme demonstrates multi-user thinking without provisioning cost.
- **ADR-003**: Plain partitioned parquet for MVP, Iceberg deferred to Phase 2 (schema
  evolution/time-travel not needed yet, reversible upgrade path stays open).
- **ADR-004**: DynamoDB for agent-facing metadata, decoupled from the pipeline, and promoted to
  **sole serving layer** (superseded Athena/Glue Catalog — the app serves data through the UI
  only, no ad-hoc SQL layer needed). Agent never touches Step Functions; reads only DynamoDB
  (`LATEST` + `WEEK#<date>` items cover both fast-path and history queries).
- **ADR-005**: Amplify + API Gateway WebSocket for serving layer. Enables true live dashboard
  updates driven by agent tool calls, not polling. Real variable cost driver is Bedrock model
  invocation (tokens), not hosting — watch conversation volume, not infra cost, against budget.
- **Glue Catalog/Athena dropped**: transform compute is Glue **Python Shell** jobs (cheap,
  pandas/boto3-scale), not Spark — data volume (KB–MB/day) doesn't justify distributed compute.
  No Glue Catalog or Athena in the architecture at all — DynamoDB (ADR-004) is the only serving
  layer; Gold parquet in S3 exists for lineage/archival only, read solely by the sync Lambda.

## Working agreements

- Spec-driven development: architecture/data-model/security decisions go through
  solution-architect → data-engineer → security-expert skills before code is written. Don't
  skip straight to implementation on new components — run the relevant skill(s) first.
- Real Hevy API schema (confirmed via their OpenAPI spec) drives all schema work — don't
  assume fields; verify against `https://api.hevyapp.com/docs/` or the mirrored OpenAPI spec if
  the shape is ever in doubt.
