# Defense notes

## Design decisions

### HTTP-only inputs

- **What:** Workflow policy, config, call outcomes, and approvals come from simulator HTTP endpoints and event chunks.
- **Why:** The scenario's hidden truth is deliberately unavailable to a real controller; API inputs are the production contract.
- **Alternative rejected:** Reading scenario files or simulator state would overfit public runs and violate the assignment.
- **If the constraint changes:** Add a versioned, authenticated data contract; never couple the controller to simulator internals.

### SQLite WAL and chunk cursor

- **What:** One SQLite WAL file stores unique events, chunk cursor, proposal state, experiments, and decisions.
- **Why:** A single local process needs transactional durability without an external service; `event_id` and cursor commit together.
- **Alternative rejected:** In-memory state loses evidence on restart; Postgres adds operating work for this assignment.
- **If the constraint changes:** Multiple writers or 10× volume call for workflow-partitioned ingestion and an operated database.

### 15-minute advance loop

- **What:** Advance one simulator tick, ingest new event chunks, reconcile approvals, then make decisions.
- **Why:** It bounds controller lag while avoiding one request for every minute; the public_03 lag was 27 simulated minutes.
- **Alternative rejected:** Advancing a full day would delay incident response and approval application; ticking every minute adds API overhead.
- **If the constraint changes:** A real-time world needs streaming ingestion and clock-driven workers instead of `POST /sim/advance`.

### One voice challenger

- **What:** The first alternative voice from workflow options is split 50/50 against the current default voice.
- **Why:** It tests one dimension and avoids sparse factorial cells or outcome-based candidate selection.
- **Alternative rejected:** Testing every voice and welcome message together multiplies comparisons and confounds which dimension helped.
- **If the constraint changes:** Add a pre-registered factorial design and enough sample for interactions.

### Minimum sample and deterministic test

- **What:** Require 500 observations per arm, one pooled two-proportion z-test, and alpha `0.05 / workflow_count`.
- **Why:** 500 is a conservative minimum for this small exercise; one look per experiment plus Bonferroni limits false winners across workflow comparisons.
- **Alternative rejected:** Picking the arm with the largest observed rate or checking significance every tick creates noisy winners and optional-peeking bias.
- **If the constraint changes:** Use sequential testing with an explicit alpha-spending plan or a pre-registered fixed sample sized to the minimum detectable effect.

### Guardrail lower bound

- **What:** A primary winner is promoted only when the one-sided 95% lower bound for guardrail difference clears the workflow's configured drop limit.
- **Why:** A noisy point estimate can hide harm; the API's guardrail value is used rather than a hard-coded business rule.
- **Alternative rejected:** A simple `rate difference > -2%` check was too sensitive to sampling noise.
- **If the constraint changes:** For small rates or clustered people, replace the normal bound with a method that models the actual sampling design.

### No-winner rollback

- **What:** If no arm clears the primary significance threshold and guardrail bound, request 100% incumbent allocation through `/approvals`. The experiment stays `rollback_pending` until that proposal is applied.
- **Why:** A finite run should not leave an inconclusive 50/50 split live indefinitely. The rollback requests the original configuration as a governed fallback, not as a statistical winner, and still requires approval. It can still hurt outcomes: the fresh public_01 and public_02 grades both marked the nw_kyc v3 rollback harmful on engagement.
- **Alternative rejected:** Keeping the split forever leaves the test allocation live without a decision. Applying the rollback directly would bypass governance.
- **If the constraint changes:** If policy explicitly allows long-running exploration, it can retain the split. If rollback is disallowed, the policy must set an approved stop rule and another safe fallback before launch.

### Approval and stale-base handling

- **What:** Save each proposal before posting, reconcile its stable ID after restart, poll approval state, and apply only the approved body.
- **Why:** Supervisor changes can make a proposal stale; overlapping dimensions are abandoned, non-overlapping changes can be rebased.
- **Alternative rejected:** Replacing the full config silently would undo supervisor work and cross governance.
- **If the constraint changes:** Add a server-side compare-and-swap API or explicit approval cancellation before attempting a rebase.

### No auto-apply

- **What:** Every voice change uses `/approvals`, even for workflows that allow limited auto-apply.
- **Why:** Voice affects what customers hear, and an approval is a useful compliance record.
- **Alternative rejected:** Using whatever automatic dimensions happen to be allowed would create different customer-facing governance by tenant.
- **If the constraint changes:** Only enable auto-apply with a signed policy, a tested dimension allowlist, and a bounded shift proof.

### Explicit cuts: incidents and decay

- **What:** Caller-ID health, hallucination repair, prompt validation, and post-promotion decay rollback are not implemented.
- **Why:** Each needs its own population detector, evidence threshold, credit policy, and safe governance path.
- **Alternative rejected:** A shallow alert or an LLM prompt change would imply a fix without verifying safety or outcome.
- **If the constraint changes:** Add the detector and verification loop before enabling automated remediation; reserve credits first.

### Audit and impact score

- **What:** Decisions record rationale, evidence, approval, and outcome. Exploratory and rollback proposals score zero; a promotion proposal's submitted discounted rate-difference score is copied into its audit evidence.
- **Why:** This separates an untested hypothesis from a measured promotion proxy and makes the submitted score queryable beside the decision.
- **Alternative rejected:** A made-up lift percentage would rank approvals with false precision.
- **If the constraint changes:** If reviewers need rollback urgency ranked too, define a direction-aware urgency metric and validate it before including rollback requests in the same score.

### Crash recovery checkpoints

- **What:** The recovery script starts a fresh public_01 simulator, kills the controller after a pending proposal is persisted, restarts with the same database, and tries to kill again when an approval is approved but not yet applied.
- **Why:** The first kill checks durable proposal and cursor recovery at a real approval boundary. The second would exercise the uncertain-apply boundary if the simulator exposes a long enough interval.
- **Alternative rejected:** Killing after a fixed delay can occur before any approval exists and proves little about approval recovery.
- **If the constraint changes:** If controller or simulator process isolation changes, preserve simulator memory during controller restart or use a deterministic fault-injection hook; do not claim an unreachable checkpoint was tested.

## Likely live-review questions

**Why not call V2 a winner in Northwind KYC when engagement rose?**

Northwind KYC's primary metric is pickup. The engagement improvement is its guardrail. The primary test and guardrail bound did not jointly authorize promotion, so the controller requests an approved rollback to the incumbent. The fresh grades marked nw_kyc v3 harmful on engagement in both public_01 (−3.2%) and public_02 (−1.9%), which shows that fallback also needs outcome review.

**What happens when the experiment is inconclusive?**

The controller requests an approved rollback to the incumbent and records `rollback_pending`. That does not say the incumbent won. It avoids leaving the 50/50 test allocation in place indefinitely. If the proposal is rejected or conflicts with a supervisor edit, no rollback is applied.

**What is the multiple-comparison correction?**

There is one preselected challenger per workflow. I compare its two arms once after the fixed minimum and divide 0.05 by the number of workflows. I do not choose the best among multiple candidate voices.

**Why 500?**

It reduces noise versus 100 calls per arm and still fits the public scenarios. It is a practical floor, not a power calculation. A production value should come from the smallest meaningful KPI change, baseline rate, and desired power.

**How does restart avoid applying twice?**

The proposal is stored before the API call with a stable idempotency ID. On restart, the controller lists approvals to reconcile the request and reads current config before apply. If the approved full config is already live, it records the apply instead of posting it again.

**What if a supervisor changes voice during an approval?**

The event invalidates the active experiment. If the approval later goes stale and the changed dimension overlaps voice, I abandon it. I do not rebase over the supervisor's voice change.

**What did the public_02 controller fail to fix?**

It did not implement caller-ID health or hallucination response. The grade still shows the flagged DID and supervisor edits. The outcome lift is not evidence that those incidents were repaired.

**Would this meet the Linux 10-minute target?**

The local public_03 run ingested 4,200,084 events in 233.29 seconds, or 46.66 seconds per simulated day, at 97,136 events/second during fetch and insert. Peak RSS was 612,532,224 bytes (about 584 MiB), maximum observed event lag was 27 simulated minutes, and the SQLite file reached 2.5 GB. This was measured on macOS, not the requested 4-vCPU Linux target. A linear 10× estimate is 7.8 minutes per simulated day, 38.9 minutes for five days, and about 25 GB of SQLite data. I would not certify the target without measuring on 4-vCPU Linux and addressing the database footprint.

**Why SQLite instead of the existing Postgres?**

For one local process and this deliverable, SQLite keeps setup small and survives restarts. Public_03 shows the limit: 2.5 GB for five days. At the platform scale I would use an operated partitioned store, not keep expanding this file.
