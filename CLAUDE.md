# Workout Coach — AI-Driven Fitness Coaching (Data Engineering Portfolio)

AI-driven workout coach. Pulls training data from Hevy API, pipelines it through a fully-AWS
serverless stack, and runs a single hypertrophy-focused Bedrock agent weekly that emails a
training analysis report. No chat UI, no multi-agent orchestration. Built as a data engineering
portfolio project — architecture choices favor demonstrating AWS-native serverless patterns over
minimizing engineering effort.

## Constraints (do not violate without re-running the relevant skill)

- **Budget**: ~$5–10/mo. This ruled out MWAA (min ~$200+/mo) and always-on Redshift.
- **Fully AWS**: no local Docker Compose components in the running system. Local dev/testing
  containers are fine; the deployed system is AWS-native only.
- **No Airflow.** Replaced by EventBridge Scheduler + Step Functions (see ADR-001). Don't
  reintroduce Airflow without revisiting that decision with the user.
- **Multi-user schema design, single-user tested.** Partition/model for multiple users even
  though only one real account exists today.
- **Single-user, no auth** — no UI at all now (email-only delivery), so no auth surface to add.

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
   (no Glue Catalog, no Athena — no UI at all, DynamoDB feeds the weekly agent job only)

Routine-weight sync (deterministic, no LLM, no confirmation — B5):
   EventBridge Scheduler (weekly) → Lambda: sync_routine_weights
     Reads gold max_weight_kg per exercise from DynamoDB, compares vs live
     GET /v1/routines target weight, PUT-updates the routine when they differ.
     Mechanical mirror of already-logged reality (not a judgment call, not a
     progression) — Hevy never writes actual logged weight back into the
     routine template itself, so this exists to close that gap. Deliberately
     NOT in Glue (Glue = pure S3 transform, no external calls) and NOT an
     agent tool (no model in the loop, nothing to gate) — own Lambda, own
     least-priv IAM role scoped to SSM read (Hevy key) + nothing else.

AI Coach (decoupled from pipeline — separate consumer, weekly, no chat):
   EventBridge Scheduler (weekly) → Lambda → Bedrock AgentCore (Strands SDK)
   ONE agent — hypertrophy-focused only, no orchestrator, no other specialists
   Tools — deterministic Python owns every decision with a numerically correct
   answer (see ADR-008); the agent only orchestrates + writes prose:
     query_workout_history                                     — read-only, DynamoDB
     find_progression_candidate, summarize_consistency,
     find_plateaus, find_fatigue_signals                       — deterministic analysis
     propose_progression                                       — write-adjacent, token-gated
   Agent judgment is scoped to muscle-group/variety categorization (no ground
   truth to compute it from) and a closing improvements summary that only
   recombines already-computed facts — everything else is code, not prompt.
   Output: one HTML analysis report, sent via SES to the user's email — no
   chat turn, so the proposal is applied through a confirm-link in the email
   (tokenized proposal_id) handled by a standalone `confirm_progression`
   Lambda, NOT the agent — the model never holds the Hevy write credential.

UI: none. No React SPA, no Amplify, no WebSocket. Delivery is the weekly email only.
```

## Data model

**Bronze** (`s3://.../bronze/workouts/user_id=<id>/ingest_date=<yyyy-mm-dd>/`) — raw Hevy JSON,
gzip, append-only, immutable. Never overwritten, never has rows dropped.

**Silver** (`silver.sets`, parquet, partitioned `user_id`/`year_month`) — one row per **set**
(finest grain). Keeps ALL sets including warmups (`is_warmup` flag, not filtered) — business
rules apply downstream, not here.

**Gold** (`gold.weekly_exercise_stats`, `gold.weekly_summary`, partitioned `user_id`/`week`) —
aggregated. This is where `is_warmup=true` sets get excluded from volume/1RM calculations.

Silver and gold have a 60-day S3 lifecycle expiration — both are fully reprocessable from
bronze at any time, so trimming them is safe and reversible. Bronze has no lifecycle rule; it's
the immutable source of truth and stays forever.

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
- **Agent tools are read-only by default, and `propose_progression` (the only write-adjacent
  one) is a proposal, not a write.** It only executes against real logged history and persists a
  `proposal_id` — it cannot accept a free-form weight/reps value from the model. Proposals
  expire after 3 days (long enough to read and click the emailed report) and are single-use
  (DynamoDB conditional update prevents replay). No chat turn exists to confirm in (email-only
  delivery) — confirmation is a tokenized link in the report email that triggers the standalone
  `confirm_progression` Lambda with that exact `proposal_id`. **The agent itself never holds
  the Hevy write credential or performs the write** — `confirm_progression` is deterministic
  code, not a model, so there's no write-adjacent agent tool at all beyond the proposal step.
  If any *additional* agent tool with write intent is ever added, stop and design an equivalent
  propose-with-token gate (proposal only, execution outside the model) before shipping it —
  free-form write parameters from the model are never acceptable.
- **`sync_routine_weights` is a separate class of write, not an agent tool.** It has no LLM in
  the loop and originates no judgment — it copies `max_weight_kg` already present in gold/
  DynamoDB (itself sourced from the user's own logged sets) into the routine template. The
  propose/apply-with-token gate exists to stop a *model* from originating an arbitrary write;
  this Lambda isn't a model and can't originate a value outside what's already logged, so it
  runs unconfirmed, weekly, deterministic. Don't fold this into `confirm_progression` or give it
  broader write scope than one PUT to `/v1/routines/{id}`.
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
- **ADR-005** *(superseded)*: originally Amplify + API Gateway WebSocket for a chat UI with live
  dashboard push. Dropped — no chat UI, no dashboard. Delivery is now a weekly email (ADR-006).
- **ADR-006**: single hypertrophy agent + weekly email, no chat UI, no multi-agent orchestrator.
  A chat interface implies real-time, per-turn interaction the user doesn't actually want here —
  a weekly batch analysis fits the actual usage pattern (check progress periodically, not
  converse). Cuts Amplify, WebSocket, chat-bridge Lambda, and the orchestrator/strength/fat-loss
  specialist agents entirely — one Bedrock AgentCore invocation per week, one system prompt.
  Progression confirmation moves from a chat reply to a tokenized link in the email, handled by
  a standalone `confirm_progression` Lambda (not the agent — see ADR-008).
- **ADR-007**: `sync_routine_weights` as a standalone deterministic Lambda, not folded into
  Glue or the agent. Closes a real gap — Hevy doesn't write logged workout weights back into
  the routine template, so routines drift stale across freeform/multi-routine logging. Runs
  unconfirmed because it has no LLM in the loop and mirrors already-logged reality; kept out of
  Glue because Glue jobs are pure S3 transform with no external network calls or Hevy
  credential in scope.
- **ADR-008**: agent tool split — deterministic Python owns every decision with a numerically
  correct answer (progression threshold, weight streak, plateau detection, fatigue signal,
  consistency trend); the agent only orchestrates those tool calls and writes prose. Originally
  the agent was given raw weekly numbers and asked to threshold/count/compare itself
  (`temperature=0` included) — on real production data it proposed a progression for an
  exercise below its own stated reps threshold, averaged reps across sets logged at different
  weights into one misleading number, proposed two exercises in one report despite a
  "one proposal only" rule, and narrated its own tool-call failures into the final report
  despite an explicit instruction not to. Prompt tightening reduced but never eliminated these.
  `find_progression_candidate`, `summarize_consistency`, `find_plateaus`, and
  `find_fatigue_signals` replaced that reasoning with unit-tested Python — same input always
  produces the same output. The agent's remaining judgment is scoped to exactly two things:
  muscle-group/variety categorization (no structured ground truth exists to compute it from —
  legitimate use of the model's world knowledge) and a closing improvements summary that only
  recombines already-computed facts into prose, never new numbers. General rule (not specific
  to this project): a task with one correct numeric answer is a deterministic tool; a task
  requiring free-text interpretation with no ground truth is legitimate agent judgment. Written
  up as a reusable pattern in the `bedrock-agentcore` Claude Code skill.
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
