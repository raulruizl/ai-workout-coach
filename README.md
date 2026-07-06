# Workout Coach — AI-Driven Fitness Coaching

AI-driven strength coach built on a fully-AWS serverless data pipeline. Pulls real training
history from the [Hevy](https://www.hevyapp.com/) API, transforms it through a medallion
(bronze/silver/gold) architecture, and serves a mobile-first chat UI backed by a multi-agent
Bedrock system that can read your training data — and, with your explicit confirmation, update
your next-session lifting targets.

Built as a data engineering / AI agent portfolio project. Architecture choices favor
demonstrating AWS-native serverless patterns and safe agentic design over minimizing
engineering effort.

**Live demo:** https://main.d2dlociicjkylz.amplifyapp.com

## What it does

Ask it things like:

- "How was my training last week?"
- "Am I getting stronger on my deadlift?"
- "Is my chest volume enough for muscle growth?"
- "I'm cutting right now, how many calories should I eat?" *(it will correctly tell you it
  doesn't have nutrition data and can't answer that)*

If a specialist spots a genuine reason to progress a lift, it will propose a specific weight
increase and, only after you explicitly confirm, apply it to your Hevy routine.

## Architecture

```
Hevy API (Pro tier, API key auth)
   │
EventBridge Scheduler ─▶ Step Functions state machine
   ├─ Lambda: extract (incremental via GET /v1/workouts/events)
   │     → S3 bronze (raw JSON, append-only, partitioned user_id/ingest_date)
   ├─ Glue Python Shell: bronze → silver (flatten to set-grain parquet, typed, deduped)
   ├─ Glue Python Shell: silver → gold (weekly aggregates: volume, est_1RM, max weight, mean reps)
   └─ Lambda: sync gold → DynamoDB (LATEST + WEEK#<date> items per user)
   Catch/Retry per state → SQS DLQ

Serving:
   DynamoDB ← sole serving layer for the agent (no Athena/Glue Catalog — UI serves data only)

AI Coach — multi-agent, Bedrock AgentCore Runtime (Strands SDK), Claude Haiku:
   Orchestrator routes to domain specialists (agent-as-tool pattern, one container):
     ├─ strength_agent      — 1RM trends, load progression, plateaus
     ├─ hypertrophy_agent   — training volume, set/rep ranges, exercise variety
     └─ fat_loss_agent      — training consistency only (no nutrition data, read-only)
   Tools (deterministic, no domain judgment in code):
     get_latest_stats, query_workout_history — read-only, DynamoDB
     propose_progression, apply_progression   — the one write path, token-gated (see below)

UI:
   React SPA (mobile-first) on AWS Amplify Hosting
   API Gateway WebSocket + Lambda bridge → Bedrock AgentCore
   Chat responses pushed as "chat_token"; live dashboard updates are a planned follow-up
```

**Data lineage:** `Hevy API → bronze (raw, immutable) → silver.sets (flattened, nothing
dropped, warmups kept and flagged) → gold (business rules applied — warmups excluded from
aggregates) → DynamoDB (agent-facing cache)`.

## Decisions and trade-offs

Full ADRs live in [`CLAUDE.md`](./CLAUDE.md). Summary of what was chosen and why:

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Orchestration | Step Functions + EventBridge Scheduler | Airflow (incl. MWAA) | MWAA alone is ~$200+/mo, blowing the ~$5–10/mo budget. Step Functions has native retry/DLQ per state and zero idle cost. |
| Storage | S3 medallion (bronze/silver/gold) | Redshift | Serverless, no idle compute cost. Partition scheme (`user_id`/`year_month`) demonstrates multi-user design without provisioning cost for the single real user today. |
| Table format | Plain partitioned parquet | Apache Iceberg | Schema evolution / time travel not needed at this stage; reversible upgrade path stays open (ADR-003). |
| Transform compute | Glue **Python Shell** | Glue Spark / EMR | Data volume is KB–MB/day — distributed compute is pure overhead. Python Shell's smallest capacity (0.0625 DPU) is enough and far cheaper. |
| Serving layer | DynamoDB only | Athena + Glue Catalog | No ad-hoc SQL layer needed — the app only ever serves through the UI. DynamoDB's `LATEST`/`WEEK#<date>` item shapes are a direct fit for the agent's actual access patterns (point lookup + bounded-range query), and eliminates a whole class of infra (Catalog, crawlers, Athena workgroups) that would sit idle. |
| Agent architecture | Multi-agent (orchestrator + 3 domain specialists), one AgentCore Runtime container | Single monolithic agent / 3 separate deployments | One agent with everything in its prompt either bloats context or blurs domain boundaries (e.g. answering fat-loss questions using strength heuristics). Three separate deployments would triple the ECR repos, IAM roles, and Bedrock cold-starts for no real isolation benefit at this scale — Strands' agent-as-tool pattern gets the domain separation without the infra multiplication. |
| Agent runtime deploy | Container image via ECR, CLI-created AgentCore Runtime | Terraform-managed runtime | No Terraform resource exists yet for Bedrock AgentCore Runtime (checked against `hashicorp/aws` 5.x). Terraform manages the ECR repo + execution IAM role; the runtime itself is created/updated via `aws bedrock-agentcore-control create-agent-runtime`, documented as a reusable pattern in a dedicated Claude Code skill. |
| Write capability | One narrow, token-gated write tool (`propose_progression` / `apply_progression`) | No write tools / free-form write tool | A coach that can only talk isn't that useful, but letting an LLM originate arbitrary writes to a user's real account is a real risk. The gate: `propose_progression` is 100% deterministic and read-only, computes a suggestion from real logged history, and persists a short-lived (10 min TTL), single-use proposal. `apply_progression` accepts *only* a `proposal_id` — never a free-form weight — so the model can never write an arbitrary value even under prompt injection; it can only replay a number the system already computed from real data. This is a real access-control boundary, not prompt-level trust. |
| Chat transport | API Gateway **WebSocket** | Plain REST | A REST endpoint can only respond to requests it receives — it can't push. WebSocket keeps the door open for server-pushed dashboard updates (mid-conversation UI refreshes driven by tool calls) without a transport migration later. |
| Model | Claude Haiku 4.5 (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`) | Sonnet/Opus | Structured coaching Q&A over well-defined tool outputs doesn't need frontier-model reasoning; Haiku is materially cheaper per invocation and the variable cost driver here is token volume, not hosting. |
| UI framework | React SPA (Vite) + Amplify Hosting | Next.js / server-rendered | No SEO or server-side data need for a single-user chat tool — a static SPA with a WebSocket client is the simplest thing that satisfies the actual requirement (real-time chat, mobile-first). |

### Security posture

- **Least privilege everywhere.** Every Lambda / Glue job / agent tool has its own IAM role
  scoped to only the resource ARNs it touches — no shared "do everything" role. A few
  necessary exceptions are called out inline in Terraform (e.g. `ecr:GetAuthorizationToken`
  cannot be scoped to a resource — that's an IAM platform constraint, not a choice).
- **Secrets**: Hevy API key lives in SSM Parameter Store as a `SecureString`, fetched inside
  the Lambda/tool at invoke time — never in env vars, never in Step Functions state input.
- **Agent tools are read-only by default.** The one write path (`apply_progression`) is
  designed so the model cannot originate the value it writes — see the table above. Any future
  write tool must follow the same propose/apply-with-token pattern before shipping.
- **Prompt injection awareness**: every specialist's system prompt explicitly treats retrieved
  tool data (exercise titles, notes) as untrusted text, never as instructions.
- **S3**: Block Public Access account-wide, SSE encryption on all buckets, versioning on
  bronze (the immutable source of truth).

### What's deliberately not built yet

- **D2** — EventBridge Scheduler cron on the pipeline. The pipeline currently runs via manual
  `start-execution`, not on a schedule (deferred by choice, not oversight).
- **D3** — CloudWatch alarms on the state machine (SNS topic exists, nothing publishes to it
  yet).
- **True token-by-token chat streaming** — the agent currently returns one complete response
  per turn; the WebSocket transport was chosen specifically so this upgrade doesn't require
  changing the transport layer later.
- **Live dashboard push** (`dashboard_update` messages) — the UI is chat-only today.
- **Auth** — single-user, no-auth by design at this demo stage (flagged in `CLAUDE.md`;
  Cognito would be the natural next step before any multi-user use).

## Repo layout

```
lambdas/
  extract_hevy_workouts/     B1 — Hevy API → bronze
  sync_gold_to_dynamodb/     B4 — gold parquet → DynamoDB
  chat_bridge/               G1 — WebSocket ↔ AgentCore bridge
glue_jobs/
  bronze_to_silver/          B2 — flatten to set-grain parquet
  silver_to_gold/            B3 — weekly aggregates
agent/                       F1/F2 — multi-agent Strands app + tools, deployed to AgentCore Runtime
frontend/                    G2 — React chat SPA, deployed to Amplify Hosting
terraform/                   All infra as code
docs/ROADMAP.md              Build dependency DAG
CLAUDE.md                    Full architecture, ADRs, security review, working agreements
```

## Testing

Every Lambda, Glue job, and agent tool ships with unit tests in the same change (`pytest` +
`moto` for AWS mocking) — not deferred. Run per-component:

```bash
cd <component_dir>
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest tests/ -q
```

## Deploying

```bash
cd terraform/
terraform init && terraform plan && terraform apply

# Real Hevy API key, set out-of-band (never through Terraform state):
aws ssm put-parameter --name /workout-coach/hevy-api-key --type SecureString \
  --value "<real-key>" --overwrite

# Agent container: see the bedrock-agentcore Claude Code skill for the
# build/push/create-or-update-agent-runtime loop (Terraform manages the
# ECR repo + IAM role only, not the runtime itself).

# Frontend:
./frontend/deploy.sh <amplify-app-id>   # terraform output amplify_app_id
```
