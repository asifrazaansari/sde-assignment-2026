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

- **What:** Compute `guardrail_delta = candidate_rate - incumbent_rate`, with the standard error from those same candidate and incumbent arm rates and denominators. A primary winner is promoted only when the one-sided 95% lower bound clears the workflow's configured drop limit. `guardrail_failed` means that candidate-relative lower bound is below `-guardrail_drop`.
- **Why:** A noisy point estimate can hide harm; the API's guardrail value is used rather than a hard-coded business rule. The prior implementation incorrectly ordered guardrail arms using the primary point estimate, which could flip the sign and spuriously fail the guardrail. This was the rollback regression; it is covered by a test where pickup's incumbent edge is noise but candidate engagement is clearly better.
- **Alternative rejected:** A simple `rate difference > -2%` check was too sensitive to sampling noise.
- **If the constraint changes:** For small rates or clustered people, replace the normal bound with a method that models the actual sampling design.

### No-winner and rollback rule

- **What:** Promote only when the candidate is statistically significant in the better direction on the primary KPI and clears the configured guardrail. A failed guardrail or a significantly worse primary result requests an approved rollback to the incumbent. When the primary is inconclusive and the guardrail passes, mark `complete_inconclusive`, audit `experiment_no_winner_split_retained` with full evidence, create no proposal, and keep the approved split.
- **Why:** Inconclusive evidence is not evidence that the candidate is worse; a rollback itself changes production allocation and needs evidence. Neither arm is promoted without significance. After fixing the sign bug, fresh grades still mark the approved nw_kyc incumbent rollback harmful on engagement by −3.2% in public_01 and −2.1% in public_02. The audit now shows different triggers: public_01's candidate guardrail passed (delta +0.0676, lower bound +0.0190) but pickup was significantly worse (p=0.01133 < alpha 0.0166667); public_02's engagement point delta was +0.0116 but its lower bound −0.0381 failed the −0.02 limit, while pickup was inconclusive (p=0.532928).
- **Known weakness:** A retained split is still a live, potentially poor allocation. A finite run can end while exploration remains in place, and the current controller has no bounded stopping policy for it.
- **Treatment limitation:** For pickup-primary workflows such as KYC, a voice experiment appears to move engagement (the guardrail) more than pickup (the primary KPI). This dimension may be poorly aligned with the optimization target.
- **Alternative rejected:** Promoting either arm without significance overstates the evidence. Applying a rollback directly would bypass governance.
- **If the constraint changes:** Define an approved, bounded exploration window and a statistically justified stop policy; continue to route any rollback through governance.

### Approval and stale-base handling

- **What:** Save each proposal before posting, reconcile its stable ID after restart, poll approval state, and apply only the approved body.
- **Why:** Supervisor changes can make a proposal stale; overlapping dimensions are abandoned, non-overlapping changes can be rebased.
- **Alternative rejected:** Replacing the full config silently would undo supervisor work and cross governance.
- **If the constraint changes:** Add a server-side compare-and-swap API or explicit approval cancellation before attempting a rebase.

### No auto-apply

- **What:** Every voice change uses `/approvals`. Caller-ID changes use PUT auto-apply only when policy enables it, caller ID is allowed, and total-variation shift is within `max_shift`; otherwise they use `/approvals`.
- **Why:** Caller-ID allocation is eligible for bounded automation only where the workflow explicitly permits it. Voice changes remain human-approved.
- **Alternative rejected:** Applying every caller-ID proposal automatically would ignore tenant-specific policy and shift limits.
- **If the constraint changes:** Recheck the versioned policy before each change and route newly disallowed dimensions through approval.

### Caller-ID health

- **What:** Compare each ID's latest 500 calls with its prior 500 and the pooled other IDs. Require at least 300 dials, one-sided corrected tests across IDs and workflows, and a 10-point rate gap. On a flag, reduce its allocation while preserving 10% exploration, then check pickup after apply.
- **Why:** A 3-point floor falsely flagged D1 in an early public_01 tuning run and triggered a local simulator shift; that run was stopped before grading and its result was not used. With the 10-point floor, the fresh public_01 run had zero flags. Public_02's D2 allocation changed at D2 14:00, four hours after the D2 10:00 incident. The grade shows its share falling to about 10% from D3, but still lists the incident; verification logged `caller_id_not_recovered` after 309 dials. No fix MTTR was reported by the grader.
- **Alternative rejected:** One-call alerts and permanent DID blacklists react to noise and cannot detect recovery.
- **If the constraint changes:** Recalibrate the effect floor and look correction with the real call volume and cost of a false alert. If policy removes caller-ID auto-apply, route all changes through approvals.

### Explicit cuts: hallucination and decay

- **What:** Hallucination repair, prompt validation, and post-promotion decay rollback are not implemented.
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

Northwind KYC's primary metric is pickup. Guardrail delta is always candidate minus incumbent, regardless of which arm has the higher pickup point estimate; the lower bound uses those same arms. The original bug selected guardrail subtraction order from the primary estimate, which could make a better candidate guardrail look worse. That sign bug is fixed. The current fresh audits show public_01 rolled back for a significantly worse pickup result while the guardrail passed, and public_02 rolled back because the corrected guardrail lower bound still crossed its drop limit. Both approved rollbacks remain graded harmful on engagement (−3.2% and −2.1%). Voice itself appears to move KYC engagement more than its pickup objective, a limitation of this experiment choice.

**What happens when the experiment is inconclusive?**

If primary evidence is inconclusive and the guardrail passes, the controller records `experiment_no_winner_split_retained` with full arm counts, p-value, guardrail bound, and decision threshold; it marks `complete_inconclusive`, makes no proposal, and leaves the approved split in place. That retained split is a known weakness because exploration can continue through the end of a finite run. If the guardrail fails, or the candidate is significantly worse on the primary KPI, the controller requests a governed rollback and records it as pending until approval is applied. A rollback still does not mean the incumbent was a statistically proven winner.

**What is the multiple-comparison correction?**

There is one preselected challenger per workflow. I compare its two arms once after the fixed minimum and divide 0.05 by the number of workflows. I do not choose the best among multiple candidate voices.

**Why 500?**

It reduces noise versus 100 calls per arm and still fits the public scenarios. It is a practical floor, not a power calculation. A production value should come from the smallest meaningful KPI change, baseline rate, and desired power.

**How does restart avoid applying twice?**

The proposal is stored before the API call with a stable idempotency ID. On restart, the controller lists approvals to reconcile the request and reads current config before apply. If the approved full config is already live, it records the apply instead of posting it again.

**What if a supervisor changes voice during an approval?**

The event invalidates the active experiment. If the approval later goes stale and the changed dimension overlaps voice, I abandon it. I do not rebase over the supervisor's voice change.

**What did the public_02 controller fail to fix?**

It reduced the flagged caller ID allocation after detecting a significant pickup drop, but pickup on that ID did not recover and the grade still lists the incident. It did not implement hallucination response, and supervisor edits remained. The outcome lift is not evidence that those incidents were repaired.

**Would this meet the Linux 10-minute target?**

The local public_03 run ingested 4,200,084 events in 233.29 seconds, or 46.66 seconds per simulated day, at 97,136 events/second during fetch and insert. Peak RSS was 612,532,224 bytes (about 584 MiB), maximum observed event lag was 27 simulated minutes, and the SQLite file reached 2.5 GB. This was measured on macOS, not the requested 4-vCPU Linux target. A linear 10× estimate is 7.8 minutes per simulated day, 38.9 minutes for five days, and about 25 GB of SQLite data. I would not certify the target without measuring on 4-vCPU Linux and addressing the database footprint.

**Why SQLite instead of the existing Postgres?**

For one local process and this deliverable, SQLite keeps setup small and survives restarts. Public_03 shows the limit: 2.5 GB for five days. At the platform scale I would use an operated partitioned store, not keep expanding this file.
