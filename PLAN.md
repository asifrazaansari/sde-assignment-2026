# Controller plan

## API surface

Use only the HTTP API described in `docs/SIMULATOR.md`: health/state and workflow/config reads; history/events chunk listing and NDJSON retrieval; `POST /sim/advance`; approval create/list/get and workflow apply; policy-permitted config auto-apply; prompt suggestion and validation only if a qualified incident path is implemented. The running server's `/docs` may clarify request/response shapes. Never read run directories, import simulator code into the controller, or inspect scenario YAML.

## Data model

Use a small standard-library Python controller with SQLite WAL. Persist fetched chunks/events, event and chunk cursors, per-workflow population aggregates keyed by cohort and actual variant, config versions, experiments, approval lifecycle, credit observations, and append-only decisions. Commit event inserts and cursor advancement in one transaction. Use deterministic proposal IDs and reconcile API approval state on restart before any action.

## Capability choices

| Capability | Choice | Reason |
|---|---|---|
| C1 | Build | Durable, idempotent ingestion is foundational and supports the load run. |
| C2 | Partial | Use the simulator's reported cohort ID consistently in call evidence and experiment grouping; defer feature-based custom cohorts and a supervisor UI. |
| C3 | Cut | Historical bootstrapping adds work before the public floor; use only post-start evidence. |
| C4 | Cut | Call-time optimisation is optional and risks fragmenting the evidence base. |
| C5 | Cut | Caller-ID rotation needs a separate safe exploration design. |
| C6 | Partial | Test one voice challenger at a time with at least 500 calls per arm and deterministic family-wise control; defer welcome-message variants. |
| C7 | Partial | Promote only supported winners; keep an inconclusive 50/50 split without promoting either arm. Defer rolling decay monitoring and post-promotion rollback. |
| C8 | Cut | Prompt incident response consumes credits and requires a separate validated safety loop. |
| C9 | Partial | Reconcile approvals, expiry/lapse and stale-base re-proposals; defer multi-change batching. |
| C10 | Partial | Provide transparent estimates and a historical sample description; avoid unsupported causal claims. |
| C11 | Cut | A UI does not improve the end-to-end floor; audit queries serve basic inspection. |
| C12 | Build | Durable own-store decisions connect reason, evidence, approver and outcome. |
| C13 | Build | Exercise kill/restart using durable cursors and approval reconciliation. |
| C14 | Partial | Record event lag, decisions and observed credit burn for run summaries. |
| C15 | Cut | Cross-tenant credit and approver scheduling is outside this single-controller first pass. |

## Assumptions

1. The controller advances simulated time in 15-minute steps and stops only when `/sim/state` reports finished.
2. Only observed API events count as evidence; pickup and engagement remain separate, and workflow objective fields define primary and guardrail.
3. A minimum population is required for detection and every experiment decision; no individual call triggers a change.
4. All configuration changes use approval or a policy-permitted auto-apply path, and supervisor edits invalidate a stale experiment base.
5. SQLite is adequate for the supplied single-process run; no external state service is introduced.
