# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.344 | +0.6% | 0.241 | -1.5% | 0.500 | +1.7% | 0% | 51 (57) |
| nw_kyc | 0.375 | +2.4% | 0.241 | -0.2% | 0.612 | +2.5% | 9% | 19 (21) |
| nw_renewal | 0.412 | -1.1% | 0.260 | -1.6% | 0.547 | +0.9% | 0% | 42 (54) |
| ALL | 0.373 | +0.3% | 0.248 | -1.3% | 0.538 | +1.6% | 2% | 112 (132) |

## Changes applied by the controller

5 applied — neutral: 3, beneficial: 1, harmful: 1

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.034 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=-0.032 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 5,
  "decisions": {
    "approved": 5,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 5,
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