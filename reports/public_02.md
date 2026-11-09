# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.246 | +4.4% | 0.183 | +1.3% | 0.479 | +2.5% | 0% | 306 (348) |
| nw_kyc | 0.373 | -0.2% | 0.238 | +1.5% | 0.600 | -1.4% | 11% | 26 (29) |
| nw_renewal | 0.441 | +1.8% | 0.264 | +0.5% | 0.561 | +1.2% | 5% | 59 (54) |
| ALL | 0.338 | +2.2% | 0.222 | +1.0% | 0.538 | +1.0% | 4% | 391 (431) |

## Changes applied by the controller

4 applied — beneficial: 1, neutral: 2, harmful: 1

- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=-0.019 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 5,
  "decisions": {
    "rejected": 1,
    "approved": 4,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 4,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.495,
      "3": 0.495,
      "4": 0.511,
      "5": 0.509,
      "6": 0.501,
      "7": 0.498,
      "8": 0.497,
      "9": 0.503
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0