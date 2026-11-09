# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.349 | +2.3% | 0.242 | -1.1% | 0.506 | +2.8% | 0% | 62 (57) |
| nw_kyc | 0.379 | +3.4% | 0.245 | +1.2% | 0.609 | +1.9% | 14% | 17 (21) |
| nw_renewal | 0.417 | +0.3% | 0.262 | -1.0% | 0.550 | +1.5% | 0% | 44 (54) |
| ALL | 0.378 | +1.8% | 0.249 | -0.6% | 0.541 | +2.2% | 3% | 123 (132) |

## Changes applied by the controller

3 applied — neutral: 2, beneficial: 1

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.034 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 3,
  "decisions": {
    "approved": 3,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 3,
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
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0