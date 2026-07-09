# Workout Coach — AI-Driven Fitness Coaching

AI-driven hypertrophy coach built on a fully-AWS serverless data pipeline. Pulls real training
history from the [Hevy](https://www.hevyapp.com/) API, transforms it through a medallion
(bronze/silver/gold) architecture, and runs a single Bedrock agent weekly that emails a
muscle-growth-focused training analysis — with an optional, one-click confirm to apply a
proposed weight progression to your next Hevy session.

Built as a data engineering / AI agent portfolio project. Architecture choices favor
demonstrating AWS-native serverless patterns and safe agentic design over minimizing
engineering effort.

## What it does

Every week, the agent reads your last few weeks of training data and emails a report covering:

- Training volume per muscle group (working sets vs the ~10–20/week hypertrophy target)
- Exercise variety (flags machine-only or repetitive weeks)
- Progress on your key lifts (rising volume with flat weight = accumulating fatigue, not growth)
- A weight-progression suggestion when double-progression criteria are met (reps have climbed
  to ~9–10 at the current weight) — click to confirm and it's applied to your Hevy routine, no
  further action needed.

Separately, a deterministic weekly sync keeps your Hevy routine's target weights in sync with
what you actually lifted last — closing a real Hevy gap where routine templates don't update
from logged workouts on their own.

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
   DynamoDB ← sole serving layer (no Athena/Glue Catalog, no UI — feeds the weekly agent only)

Routine-weight sync (deterministic, no LLM, weekly, no confirmation needed):
   EventBridge Scheduler ─▶ Lambda: sync_routine_weights
     gold max_weight_kg per exercise vs live GET /v1/routines target → PUT when they differ

AI Coach — single hypertrophy agent, Bedrock AgentCore Runtime (Strands SDK), Claude Haiku:
   EventBridge Scheduler (weekly) ─▶ Lambda ─▶ agent ─▶ SES email
   Tools — all decision logic is deterministic Python, the agent only orchestrates + narrates:
     query_workout_history                                        — read-only, DynamoDB
     find_progression_candidate, summarize_consistency,
     find_plateaus, find_fatigue_signals                          — deterministic analysis, zero LLM math
     propose_progression                                          — the one write-adjacent tool, token-gated
   Agent judgment is scoped to exactly two things: writing prose, and muscle-group/variety
   categorization (the one place with no structured ground truth to compute from — see
   "Agent vs deterministic tools" below). No chat UI, no orchestrator, no other specialist
   agents — one agent, one weekly run.
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
| Serving layer | DynamoDB only | Athena + Glue Catalog | No ad-hoc SQL layer needed — nothing serves data except the weekly agent job. DynamoDB's `LATEST`/`WEEK#<date>` item shapes are a direct fit (point lookup + bounded-range query), eliminating a whole class of infra (Catalog, crawlers, Athena workgroups) that would sit idle. |
| Agent architecture | One hypertrophy agent, weekly batch run | Multi-agent orchestrator + chat UI | A chat interface implies real-time conversation the actual usage pattern doesn't need — a weekly report fits how the user actually checks progress. Cuts Amplify, WebSocket, chat-bridge Lambda, and two specialist agents (strength/fat-loss) with no loss for the hypertrophy-only use case (ADR-006). |
| Agent runtime deploy | Container image via ECR, CLI-created AgentCore Runtime | Terraform-managed runtime | No Terraform resource exists yet for Bedrock AgentCore Runtime (checked against `hashicorp/aws` 5.x). Terraform manages the ECR repo + execution IAM role; the runtime itself is created/updated via `aws bedrock-agentcore-control create-agent-runtime`, documented as a reusable pattern in a dedicated Claude Code skill. |
| Progression write capability | `propose_progression` (agent tool, proposal only) + `confirm_progression` (standalone deterministic Lambda, does the actual write), confirmed via emailed link | Model-facing apply tool / free-form write tool | A coach that can only talk isn't that useful, but letting an LLM originate arbitrary writes to a user's real account is a real risk. `propose_progression` is 100% deterministic and read-only, computes a suggestion from real logged history, and persists a short-lived (3-day TTL, click-window for the emailed report), single-use proposal — the model never touches Hevy itself. `confirm_progression` accepts *only* a `proposal_id` from the emailed link — never a free-form weight — and it isn't a model at all, so there's no prompt-injection surface on the write path whatsoever. |
| Routine-weight sync | Standalone deterministic Lambda, unconfirmed | Fold into Glue, or route through the agent/write-gate | It's a mechanical mirror of already-logged reality (last actual weight → routine target), not a judgment call — no LLM originates the value, so the propose/apply risk model doesn't apply. Kept out of Glue because Glue jobs are pure S3 transform with no external calls or Hevy write credential in scope (ADR-007). |
| Model | Claude Haiku 4.5 (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`) | Sonnet/Opus | Structured coaching analysis over well-defined tool outputs doesn't need frontier-model reasoning; Haiku is materially cheaper per invocation and the variable cost driver here is token volume, not hosting. |
| Delivery | Weekly email (SES) | Chat UI / dashboard | No UI to build, host, or secure. Matches the actual usage pattern (periodic check-in, not conversation) and removes an entire infra layer (Amplify, WebSocket, frontend build). |
| Agent tool design | Deterministic Python tools decide everything with a numerically correct answer (progression threshold, weight streak, plateau, fatigue, trend labels); the agent only orchestrates tool calls and writes prose | Give the model raw weekly numbers and let it reason/threshold in its own head | Tried the second approach first — it failed on real production data: proposed a weight increase for an exercise whose reps were below the stated threshold, averaged reps across sets logged at *different* weights into one misleading number, violated its own "one proposal per report" rule, and narrated its own tool-call failures into the final report despite explicit instructions not to. Tightening the prompt reduced but never eliminated these. Moving the arithmetic into testable Python (`find_progression_candidate` etc.) fixed all of them — see "Agent vs deterministic tools" below for the general rule and ADR-008. |

### Agent vs deterministic tools

The rule that came out of the failures above: **if a task has one numerically correct answer
(counting, comparing, thresholding, picking a max/min, trend direction) it's a deterministic
Python tool, full stop** — no amount of prompt tightening makes an LLM reliably do that
arithmetic over raw JSON, even at `temperature=0`. **If a task requires interpreting free text
with no structured ground truth** (inferring a muscle group from an exercise's name — nothing
in the data model says "Aperturas (Máquina) = chest") **that's legitimate agent judgment**,
because no amount of code replaces the missing world knowledge without adding a new data
source. Text synthesis that only recombines already-computed facts into prose (the closing
"possible improvements" section, drawing on prior tool outputs without inventing new numbers)
is also fine for the model, since the non-determinism there is in wording, not in facts, and
nothing downstream depends on it being byte-identical run to run.

Concretely in this codebase: `find_progression_candidate`, `summarize_consistency`,
`find_plateaus`, and `find_fatigue_signals` are 100% deterministic — same 4 weeks of data
always produces the same result, independently unit-tested with `moto`, no Bedrock call
involved in the decision itself. The agent's system prompt explicitly forbids it from
re-deriving or questioning what these tools return. The only sections where the model exercises
real judgment are muscle-group/variety categorization (no tool backs it — deliberately
qualitative, not exact arithmetic) and the closing improvements summary. This split is written
up as a general pattern (not project-specific) in the `bedrock-agentcore` Claude Code skill,
since it applies to any Strands/AgentCore agent, not just this one.

### Security posture

- **Least privilege everywhere.** Every Lambda / Glue job / agent tool has its own IAM role
  scoped to only the resource ARNs it touches — no shared "do everything" role. A few
  necessary exceptions are called out inline in Terraform (e.g. `ecr:GetAuthorizationToken`
  cannot be scoped to a resource — that's an IAM platform constraint, not a choice).
- **Secrets**: Hevy API key lives in SSM Parameter Store as a `SecureString`, fetched inside
  the Lambda/tool at invoke time — never in env vars, never in Step Functions state input.
- **Agent tools are read-only, and `propose_progression` (the only write-adjacent one) only
  proposes** — it never touches Hevy itself. The actual write happens in `confirm_progression`,
  a standalone deterministic Lambda triggered by the emailed link's `proposal_id`, so the model
  never holds the Hevy write credential at all. `sync_routine_weights` is a separate, non-agent,
  unconfirmed write — see ADR-007 for why that's a different risk class. Any future *agent* tool
  with write intent must follow the same propose-only, execution-outside-the-model pattern.
- **Prompt injection awareness**: the agent's system prompt explicitly treats retrieved tool
  data (exercise titles, notes) as untrusted text, never as instructions.
- **S3**: Block Public Access account-wide, SSE encryption on all buckets, versioning on
  bronze (the immutable source of truth).

### What's deliberately not built yet

- **D2** — EventBridge Scheduler cron on the pipeline. The pipeline currently runs via manual
  `start-execution`, not on a schedule (deferred by choice, not oversight).
- **D3** — CloudWatch alarms on the state machine (SNS topic exists, nothing publishes to it
  yet).
- **Auth** — moot for now: single-user, no UI at all, delivery is email-only.

## Repo layout

```
lambdas/
  extract_hevy_workouts/     B1 — Hevy API → bronze
  sync_gold_to_dynamodb/     B4 — gold parquet → DynamoDB
  sync_routine_weights/      B5 — deterministic Hevy routine-weight sync, weekly, unconfirmed
  weekly_report/             invokes the agent, renders HTML, sends via SES, weekly
  confirm_progression/       Function URL target for the email's one-click confirm link
glue_jobs/
  bronze_to_silver/          B2 — flatten to set-grain parquet
  silver_to_gold/            B3 — weekly aggregates
agent/                       Single hypertrophy agent + tools, deployed to AgentCore Runtime
terraform/                   All infra as code
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
```
