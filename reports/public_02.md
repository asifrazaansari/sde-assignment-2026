# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.243 | +3.3% | 0.181 | -0.1% | 0.481 | +2.9% | 0% | 297 (348) |
| nw_kyc | 0.385 | +3.1% | 0.241 | +2.9% | 0.609 | +0.1% | 23% | 32 (29) |
| nw_renewal | 0.434 | +0.3% | 0.265 | +0.9% | 0.552 | -0.5% | 5% | 71 (54) |
| ALL | 0.337 | +1.9% | 0.221 | +0.9% | 0.537 | +0.9% | 5% | 400 (431) |

## Changes applied by the controller

2 applied — beneficial: 1, neutral: 1

- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 3,
  "decisions": {
    "rejected": 1,
    "approved": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 2,
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
      "5": 0.508,
      "6": 0.495,
      "7": 0.491,
      "8": 0.507,
      "9": 0.49
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