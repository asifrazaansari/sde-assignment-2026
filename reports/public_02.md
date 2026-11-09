# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.268 | +13.8% | 0.201 | +11.1% | 0.471 | +0.6% | 0% | 346 (348) |
| nw_kyc | 0.365 | -2.3% | 0.235 | +0.2% | 0.596 | -2.1% | 11% | 21 (29) |
| nw_renewal | 0.431 | -0.4% | 0.264 | +0.7% | 0.549 | -1.0% | 5% | 66 (54) |
| ALL | 0.343 | +3.7% | 0.230 | +4.6% | 0.527 | -1.1% | 4% | 433 (431) |

## Changes applied by the controller

4 applied — beneficial: 2, neutral: 1, harmful: 1

- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.384 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=-0.021 didΔ=+0.000 → **harmful**

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
      "2": 0.261,
      "3": 0.102,
      "4": 0.099,
      "5": 0.094,
      "6": 0.094,
      "7": 0.104,
      "8": 0.099,
      "9": 0.106
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