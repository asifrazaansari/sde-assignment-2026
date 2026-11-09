# Outcome-First Agent Loop: Submission

**Name:** Asif Raza Ansari · **Date:** 2026-11-09 · **Hours actually spent:** 4+ hours today · **Language / storage:** Python 3 / SQLite WAL

## 0. TL;DR

The controller ingests live chunks idempotently, runs governed voice experiments, and monitors caller-ID pickup from API call events.
Fresh public_01 finished at +0.3% concluded leads versus baseline; public_02 finished at +3.7%.
Five changes were applied in each run. Each run had one harmful applied change: the approved nw_kyc voice rollback (engagement −3.2% in public_01 and −2.1% in public_02).
The caller-ID allocation was reduced after the public_02 incident, but the flagged ID did not recover. Hallucination response, call-time optimization, and a supervisor UI are not built.

## 1. Assumptions

1. The API response is the authority for workflow options, objective, current config, and governance policy. The controller does not infer hidden scenario truth.
2. Pre-D0 history is deliberately skipped; experiments use only observed post-start calls.
3. Each run uses one SQLite database file. Set `CONTROLLER_DB` when running a different simulator in the same directory.
4. A default-cohort voice experiment is a useful first unit of evidence. Custom feature cohorts and interactions between welcome message and voice are deferred.
5. Approval requests should be sent early in the local approver window. Kestrel requests start no earlier than 11:00, honoring the supervisor's stricter request.
6. `engagement_rate` is engaged connected calls divided by connected calls. `pickup_rate` is connected calls divided by dials. They are never combined.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | Built | Chunk cursor plus unique `event_id`, atomic SQLite writes, and public_03 measured ingestion. | Move from one process to partitioned workflow workers and benchmark on target Linux. |
| C2 | Shared cohorts | Partial | All call evidence uses the API `cohort_id`; experiments use `default`. No custom feature join or view. | Join lead feature records into a reusable cohort table and expose it to a supervisor UI. |
| C3 | History bootstrap | Cut | The first live run is simpler to recover and reason about. | Seed aggregates from history chunks with identical idempotent ingestion. |
| C4 | Call-time optimization | Cut | Per-person schedules require a separate experiment and create more state. | Test stable preferred-hour effects with a sufficiently large cohort and governance. |
| C5 | Caller-ID health | Partial | Uses 500-call per-ID windows, a sudden-drop check against the ID's prior window, corrected significance tests, a 10-point minimum effect, governed allocation changes, and a 10% exploration share. public_01 had zero flags. public_02's D2 share dropped, but the grader still reports the incident and the audit found no recovery. | Calibrate with more incident and no-incident runs, improve verification timing, and measure resolution under production traffic. |
| C6 | Voice experiments | Partial | One challenger, 50/50 allocation, 500 observations per arm, deterministic test. Welcome × voice interaction is not tested. | Add one-factor-at-a-time or factorial design with more sample and a pre-registered stopping rule. |
| C7 | Promotion / decay / rollback | Partial | Promote only a significant primary winner whose one-sided guardrail bound clears policy. Roll back through approval if the guardrail fails or the candidate is significantly worse on the primary KPI. If the primary is inconclusive and the guardrail passes, retain the approved split, record `complete_inconclusive` with full evidence, and create no rollback proposal. A retained split is a known weakness. No post-promotion decay detector. | Add a bounded, pre-approved stopping policy for retained inconclusive splits and sequentially valid monitoring with decay-triggered rollback after promotion. |
| C8 | Hallucination response | Cut | LLM suggestions and validation spend credits, and prompt safety is not the same as engagement. | Population detector, credit reserve, prompt validation, approved apply, and post-apply verification. |
| C9 | Governance client | Partial | Durable approval IDs, poll/reconcile, early-hour queue, expiry re-proposal, stale-base handling, and conflict abandonment. No multi-change batch composer. | Batch compatible changes and test every lifecycle transition under injected failures. |
| C10 | Impact / dry run | Partial | Affected leads use the API daily-batch size; dry run says it is an observed-event count, not a causal replay. Impact is zero without observed arm data and otherwise a discounted rate-difference ranking proxy. | Replay comparable cohorts and validate the score against actual outcomes. |
| C11 | Supervisor view | Cut | Queryable audit is the minimum useful surface for this run. | Add a read-only API or UI for proposals, experiments, and changes. |
| C12 | Audit store | Built | SQLite records evidence, rationale, approval identity, and result. | Add retention controls and a supported export path. |
| C13 | Crash recovery | Built | Demonstrated kill/restart with durable chunk cursor and proposal reconciliation. | Repeat under process, host, and disk failure on Linux. |
| C14 | Observability | Partial | Finish output reports event rate, lag, runtime, and observed credits. | Add persistent metrics and actionable alerts. |
| C15 | Fair credit allocation | Cut | No LLM or validation calls are made, so the run spends zero credits. | Add tenant-level quotas and reserve accounting if incident response is added. |

I chose a small, governed experiment and durable ingestion before adding more knobs. The caller-ID path reduced the flagged allocation in public_02, but it did not restore pickup on that ID. Supervisor edits also remained unresolved.

## 3. Stakeholders

### 01: Sales

- **Accepted:** speed where evidence is ready, and a clear per-workflow outcome summary.
- **Pushed back:** pickup and engagement cannot be blended. A persuasive prompt does not transfer between clients without validation and approval.
- **Wrong claim:** Kestrel does not permit every change by auto-apply. Its API policy limits dimensions and caps distribution shift. Northwind KYC has `auto_apply.enabled: false`; renewal only allows schedule and caller ID.
- **Reply:** “I can show a separate pickup and engagement result for each workflow, with the business outcome above them. The live policy does not permit auto-apply for Northwind KYC and limits Kestrel to specific dimensions and a small shift. This controller uses human approval for voice experiments. I will not reuse another client's prompt without validation evidence and the right approval.”

### 02: Northwind Compliance

- **Accepted:** human approval for customer-facing voice changes, validation evidence for prompts, and a durable record of config version and call variant.
- **Pushed back:** no auto-apply latitude is assumed for KYC. The workflow API says KYC auto-apply is disabled, even for timing or caller ID. Renewal permits only schedule and caller ID.
- **Wrong claim:** the memo's operational latitude cannot be generalized to all Northwind workflows; the per-workflow API policy is narrower for KYC.
- **Reply:** “Voice changes go through `/approvals`. The call event stores its workflow, config version, cohort, and actual variant; the proposal and audit record link the reason and approval. KYC policy has auto-apply disabled, so I will route any KYC change through approval. Renewal's permitted automatic dimensions are schedule and caller ID only.”

### 03: Platform / SRE

- **Accepted:** idempotent ingestion, a kill/restart demonstration, and operational numbers that distinguish measured performance from estimates.
- **Pushed back:** this take-home uses one local SQLite WAL file; it does not claim to be a production HA database. Public_03 was measured on the available macOS host, not the requested 4-vCPU Linux target.
- **Wrong claim:** none of the SRE constraints was contradicted by the API. The 10-minute target still needs a target-host measurement.
- **Reply:** “The controller stores chunk and event cursors transactionally and reconciles approvals after restart. The public_03 run processed 4,200,084 live events in 233.29 seconds with 612,532,224 bytes peak RSS, about 584 MiB, on the available macOS host. I have not certified that result on the requested 4-vCPU Linux target. Before production, I would move event storage to an operated database and add disk, lag, and retry alerts.”

### 04: Kestrel Operations Supervisor

- **Accepted:** no Kestrel requests before 11:00, at most one normal experiment request per workflow per day, approval before any change, and never overwriting a same-dimension supervisor edit.
- **Pushed back:** the API permits four requests per 24 hours, but the controller voluntarily stays below the supervisor's requested two. There is no consolidated screen in this scope.
- **Wrong claim:** the API policy does not enforce a two-request limit; that is the supervisor's preference, which the controller honors. The API permits limited auto-apply but the controller chooses approval for voice.
- **Reply:** “I will submit Kestrel requests at or after 11:00 and keep the normal cadence below two a day. Voice changes wait for approval. If your edit touches the same dimension as a queued or stale proposal, I abandon that proposal. This version has a SQLite audit query rather than a screen; a single read-only supervisor view is a next step.”

### 05: Product

- **Accepted:** separate KPIs, the server-reported cohort ID, population thresholds, evidence from calls that actually ran, and workflow-local promotion.
- **Pushed back:** call-time, caller-ID health, and a single consolidated UI are not in the floor implementation.
- **Wrong claim:** the API models `expired` as a distinct lifecycle status, not `rejected`. The controller treats it as terminal and creates a fresh proposal with a new ID and current config when appropriate.
- **Reply:** “Pickup and engagement remain separate, and every experiment uses the cohort ID reported on the call. The decision uses only calls observed on that config version. An expired approval has its own API status; I retire it and create a fresh proposal rather than reusing its ID or stale base. Promotion stays within the workflow.”

### 06: CTO

- **Accepted:** explainability before autonomy, minimal credit use, honest scale limits, and explicit cuts.
- **Pushed back:** a positive aggregate result is not enough to claim the incidents are solved. Public_02 still reports a flagged caller ID and supervisor edits.
- **Wrong claim:** “Credits are cheap here” understates the enforced budget. The scenario starts with 200 credits, `GET /sim/state` returns `credits_remaining`, and the simulator returns HTTP 402 when credits are exhausted. README constraint 7 says credits are finite and running out mid-incident is a failure.
- **Reply:** “This run spent zero credits, which kept it within budget, but I did not build hallucination response. LLM and validation calls consume a finite balance, so I would reserve credits for incident work and expose the remaining balance in the on-call view. Public_02 ended +3.7% on concluded leads against baseline. The controller reduced the flagged caller ID's share from 50% to 10%, but the grade still reports the DID incident and the pickup verification did not show recovery. Supervisor edits were also unresolved. At 10× volume the current SQLite footprint projects to about 25 GB and the five-day run to about 38.9 minutes. I would partition ingestion and move the event store before expanding autonomy.”

## 4. Architecture

```text
GET state/workflows -> POST advance (15 minutes)
                           |
                           v
                  list live event chunks after cursor
                           |
                           v
                SQLite transaction: unique events + cursor
                           |
                           v
          per-workflow, per-cohort, per-voice aggregates
                           |
             primary test + guardrail confidence bound
                           |
             proposal -> approval polling -> apply
                           |
                           v
                 append-only decision audit
```

SQLite WAL contains raw event bodies, event IDs, a global chunk cursor, serialized experiment state, proposals, and decision records. On restart the controller resumes from that chunk cursor, polls saved approvals, checks current config before applying an already-approved proposal, and abandons any stale proposal whose dimensions conflict with a supervisor edit. Pre-D0 history is not ingested.

The controller's only non-standard-library runtime dependency is `httpx`, which is already declared in the assignment requirements and provides the HTTP client. SQLite, JSON, statistics, hashing, and persistence use Python's standard library. `numpy`, `pyyaml`, `fastapi`, and `uvicorn` are existing simulator dependencies; `pytest` runs the controller tests. No dependency was added.

## 5. The learning loop

- **Cohorts:** use the simulator's `cohort_id`; this implementation evaluates `default` only. It does not create custom feature cohorts, so traffic is not fragmented across hand-built segments.
- **Experiment:** one voice challenger only. The default cohort is split 50/50 after approval. Welcome message × voice interactions are not tested.
- **Winner:** require 500 dials/call observations per arm for each relevant denominator; one pooled two-proportion z-test per workflow. The per-workflow threshold is `0.05 / number_of_workflows`, a Bonferroni correction for the one preselected challenger across workflows. There is one decision look per experiment, avoiding repeated-peeking inflation.
- **Guardrail and no-winner decisions:** read the workflow's configured drop limit. A primary winner is promoted only when it is statistically significant in the better direction and the candidate's one-sided 95% guardrail lower bound clears the negative drop limit. A failed guardrail still requests an incumbent rollback through `/approvals`. A candidate that is significantly worse on the primary KPI also requests that governed rollback. When the primary is inconclusive and the guardrail passes, mark `complete_inconclusive`, audit `experiment_no_winner_split_retained` with the full evidence, create no proposal, and leave the approved 50/50 allocation in place. Neither arm is promoted without significance. Retaining the split is a known weakness: a finite run can end with exploration still live and that allocation can have poor outcomes.
- **Decay and rollback:** only the guardrail-failure or significant-primary-regression rules above request an incumbent rollback; inconclusive evidence alone does not. A later rejection, expiry, request cap, or supervisor conflict can prevent a rollback from being applied. A post-promotion sequential decay detector and rollback path are not built.
- **Call time:** not optimized. Caller-ID monitoring is implemented, but public_02's flagged ID did not recover after its allocation was reduced.

The minimum is a judgment call. It costs time: Northwind KYC reached the 500-call minimum late enough to leave less time for experimentation, but it reduces the chance of treating a small sample fluctuation as a winner. This implementation does not claim an estimated false-winner rate from these two public runs.

## 6. Governance

- The controller saves a proposal before posting it, then stores the returned approval ID. It polls `pending`, applies only `approved`, records `rejected`, `expired`, and `lapsed`, and re-proposes an expiry with a fresh proposal ID and base when no same-dimension supervisor edit occurred.
- Requests are sent in the first two hours of the permitted approver window; Kestrel starts at 11:00. One change is proposed at a time per workflow, so the controller stays below stated request limits without a batching implementation.
- Voice experiments always use `/approvals`. Caller-ID changes use `PUT /workflows/{id}/config` only when workflow policy enables auto-apply, caller ID is in `allowed_dims`, and total-variation shift is within `max_shift`; otherwise they use `/approvals`.
- Guardrail-failure and significantly-worse-primary rollbacks use the same `/approvals` lifecycle, approver window, API request cap, stale-base re-proposal, and supervisor-edit conflict rules as other proposals. An inconclusive primary with a passing guardrail creates no rollback proposal; it records the full evidence in `experiment_no_winner_split_retained` and leaves the approved allocation in place.
- `affected_leads` is the API's daily batch size, a nominal scope estimate. `dry_run` reports the count of already observed calls and explicitly says it is not a causal replay. Exploratory and rollback proposals carry an impact score of zero because neither promotes a measured effect. Promotion proposals carry the discounted rate-difference proxy `abs(rate difference) × (1 - p-value) × 10`, capped at one; it is a queue-ranking proxy, not a forecast. The same score sent to `/approvals` is recorded in audit evidence; rollback decisions and retained-split decisions preserve their full evidence in the corresponding audit row.
- Supervisor edits are recorded. Any edit invalidates active experiment evidence. If a stale approval's changed dimension overlaps a supervisor edit, it is abandoned; otherwise the intended dimension is rebased on the latest config and re-proposed.

## 7. Incidents

No hallucination detector, prompt suggestion, or prompt validation path is implemented. No LLM or validation credits were spent; public_01 ended with 200 credits and public_02 with 200. Public_02's grade records the caller-ID flag at D2 10:00. The controller detected the D2 pickup drop at 11:45 and applied the governed allocation change at 14:00, four hours after the incident began. The grade shows the flagged ID share falling from 26.1% on D2 to about 10% from D3 onward, but it still reports the DID incident. The grade does not provide a fix MTTR, and the verification audit says pickup on the flagged ID did not recover. The Kestrel supervisor edit at D3 11:30 and Northwind supervisor edit at D5 15:00 also remain unresolved.

## 8. Scale

See [reports/public_03_load.md](reports/public_03_load.md). The final measured run ingested 4,200,084 live events at 97,136 events/second during fetch and insert, used 612,532,224 bytes peak RSS (about 584 MiB), and took 233.29 seconds end to end (46.66 seconds per simulated day). The maximum observed event-to-controller lag was 27 simulated minutes. The SQLite file reached 2.5 GB. This was measured on the available macOS host, not the requested 4-vCPU Linux target.

At 10×, linear projections put the five-day run near 38.9 minutes and storage near 25 GB; one simulated day projects to 7.8 minutes, below the 10-minute target on this host. Those estimates need a run on the requested 4-vCPU Linux target and will vary with disk throughput. The current single database is the clear storage limit. Partition event processing by workflow, then tenant, and move persistence to a managed service before adding workers.

## 9. Reliability

The script [scripts/crash_recovery.sh](scripts/crash_recovery.sh) starts a fresh public_01 simulator and kills the controller after observing a pending approval. It restarts with the same database while the simulator stays alive. On the recorded run, all three proposals reached terminal applied status, there were three unique approval IDs and zero duplicate applies, the cursor advanced from 12 at the first kill to 392 at completion, and the simulator finished. The approved-but-unapplied kill point was not observed before public_01 completed, as recorded in [reports/crash_recovery.log](reports/crash_recovery.log). Tests verify duplicate event IDs are ignored, the cursor survives reopen, a pending proposal survives reopen, and reconciliation does not create a second proposal.

GETs and non-advance POSTs retry network errors and 5xx with capped backoff. Advance is not blindly retried: after an uncertain result, the controller checks `/sim/state` and ingests any chunk already produced. Ordinary 4xx errors stop the run; finished, stale, duplicate proposal, and lapsed statuses have explicit handling.

## 10. Audit & observability

Example SQLite query for an applied change:

```sql
SELECT d.ts, p.workflow_id, d.why,
       json_extract(p.body, '$.diff') AS diff,
       d.approver, d.outcome
FROM decisions d
JOIN proposals p
  ON json_extract(d.evidence, '$.proposal_id') = p.proposal_id
WHERE d.workflow_id = 'nw_kyc'
  AND d.action = 'change_applied'
ORDER BY d.id
LIMIT 1;
```

Observed output:

```text
2027-01-04T15:15:00+05:30 | nw_kyc | Test voice V2 against V1 on the default cohort.
{"voice":{"from":{"V1":1.0},"to":{"V1":0.5,"V2":0.5}}}
simulated human approver | applied
```

Page on a cursor that stops advancing, repeated 5xx retries, disk near full, an approval near expiry, or an apply conflict. Do not page on a single poor call or a small KPI fluctuation.

## 11. Results

### public_01

| workflow | conclusion rate | Δ | pickup rate | Δ | engagement rate | Δ |
|---|---:|---:|---:|---:|---:|---:|
| ks_cancel | 34.4% | +0.6% | 24.1% | -1.5% | 50.0% | +1.7% |
| nw_kyc | 37.5% | +2.4% | 24.1% | -0.2% | 61.2% | +2.5% |
| nw_renewal | 41.2% | -1.1% | 26.0% | -1.6% | 54.7% | +0.9% |
| ALL | 37.3% | +0.3% | 24.8% | -1.3% | 53.8% | +1.6% |

Fresh grade: five changes were applied—one beneficial, three neutral, and one harmful. The approved `nw_kyc` incumbent voice rollback (`v3`) was harmful on engagement (Δ −0.032, or −3.2%). No incident was present. ALL conclusion rate was 0.373 (+0.3%), pickup 0.248 (−1.3%), engagement 0.538 (+1.6%), and engagement gap captured 2%.

Applied changes and grades: `ks_cancel v2` voice neutral (engageΔ +0.000, didΔ +0.000); `nw_renewal v2` voice neutral (+0.000, +0.000); `nw_kyc v2` voice beneficial (+0.034, +0.000); `ks_cancel v3` voice neutral (+0.000, +0.000); `nw_kyc v3` voice rollback harmful (−0.032, +0.000).

### public_02

| workflow | conclusion rate | Δ | pickup rate | Δ | engagement rate | Δ |
|---|---:|---:|---:|---:|---:|---:|
| ks_cancel | 26.8% | +13.8% | 20.1% | +11.1% | 47.1% | +0.6% |
| nw_kyc | 36.5% | -2.3% | 23.5% | +0.2% | 59.6% | -2.1% |
| nw_renewal | 43.1% | -0.4% | 26.4% | +0.7% | 54.9% | -1.0% |
| ALL | 34.3% | +3.7% | 23.0% | +4.6% | 52.7% | -1.1% |

Fresh grade: five changes were applied—two beneficial, two neutral, and one harmful. The approved `nw_kyc` incumbent voice rollback (`v3`) was harmful on engagement (Δ −0.021, or −2.1%). One request was rejected by the simulated approver. `ks_cancel v4` reduced D2's allocation and was graded beneficial on the DID metric. D2 still had a 10% exploration share, and the grader continued to report the incident. The grade shows no DID MTTR. ALL conclusion rate was 0.343 (+3.7%), pickup 0.230 (+4.6%), engagement 0.527 (−1.1%), and engagement gap captured 4%.

Applied changes and grades: `nw_kyc v2` voice beneficial (engageΔ +0.022, didΔ +0.000); `ks_cancel v2` voice neutral (+0.000, +0.000); `ks_cancel v3` voice neutral (+0.000, +0.000); `ks_cancel v4` caller_id beneficial (+0.000, +0.384); `nw_kyc v3` voice rollback harmful (−0.021, +0.000).

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| LLM prompt fixing | Public_02 has a script incident | Costs credits and requires a separate safety-and-engagement validation loop; cut for this run. |
| Auto-apply voice | Could make experiments faster | Human approval is clearer for customer-facing changes; all voice changes use `/approvals`. |
| More cohorts and per-person schedules | Could improve specific segments | Would reduce sample size per arm and add state before the base experiment is proven. |
| Postgres service | Already available to platform teams | SQLite is enough for a local single-process assignment; public_03 shows when this choice stops scaling. |

## 13. Known weaknesses

The fresh public results show lift in concluded leads but not consistently across every KPI or workflow. The approved `nw_kyc v3` incumbent rollback was graded harmful on engagement in both runs: −3.2% in public_01 and −2.1% in public_02. This is a concrete failure mode, despite the revised directional rule: the guardrail-failure / significant-primary-worse branch can still approve a rollback whose measured engagement outcome is worse. Inconclusive primary evidence with a passing guardrail instead retains the approved split and does not propose a rollback. That retained split is a known weakness: the run can finish with exploration still active and a poor allocation still exposed. The fixed sample and one-sided normal bound are intentionally simple, not a substitute for a pre-registered production experiment system. A permitted rollback remains subject to approval and can be rejected, delayed, or abandoned after a conflicting supervisor edit. Caller-ID mitigation reduced D2's allocation in public_02, but follow-up evidence showed pickup on D2 had not recovered and the grade still listed the incident without an MTTR. There is no post-promotion sequential decay monitor, hallucination response, custom cohort join, batching UI, or production Linux benchmark. Public_02 also had a rejected approval and unresolved supervisor edits. A single SQLite file is the first scale limit.

## 14. If this went to production at Formi next month

1. Freeze auto-apply and shadow-run against production events; verify the event schema, objective thresholds, and audit joins with Compliance.
2. Replace SQLite with an operated, partitioned event store and exercise restart, duplicate requests, stale approvals, and disk recovery on the target Linux machine.
3. Calibrate the caller-ID detector against more incident and no-incident traffic, then add a hallucination detector with explicit credit reserves and post-change verification.
4. Add a supervisor view for active experiments, pending approvals, variants, and reasons; keep every action queryable.
5. Run a staged rollout with per-workflow primary/guardrail review, decay monitoring, rollback approval, and measured load at 10×.
