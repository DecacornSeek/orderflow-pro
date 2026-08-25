# Trading Methodology — Steps 5–8: From Global Context to Trade Proof

**Status:** Canonical reference. Every concept below maps to a deterministic module
so it can feed the Strategy Engine (Sprint C) as a backtestable feature.

**System layering (the whole point):**

```
Layer 1  DATA PIPELINE      trades/L2 streams               (agents/exchange_*)
Layer 2  STRUCTURE          candles, volume profiles         (core/candle_classifier,
                                                              core/volume_profile,
                                                              core/session_profile,
                                                              core/weekly_profile)
Layer 3  INTERPRETATION     shapes, single prints, zones,    (core/profile_shape,
         (this document)    weak/strong extremes, road map    core/profile_structure,
                                                              core/business_zones,
                                                              core/road_map)
Layer 4  STRATEGY           entfaellt — das System beschreibt Zustand,
                            es entwickelt keine Strategien (Charter §2)
Layer 5  VALIDATION         entfaellt mit Layer 4
```

**Design rule for every module in Layer 3:** pure functions over profile data
(VAP dicts / ProfileSnapshots), no wall clock, no LLM judgment, stable dict
output — identical results live and in replay. That is what makes the strategy
layer testable.

---

## Step 5 — Weekly Profiles (Global Money Flow)

Analyze weekly volume profiles to understand large participants on longer
timeframes.

| Concept | Definition | Module / Feature |
|---|---|---|
| Trend stability | Trend drawn only from weekly VPOC + value areas: is global money flow rising, falling, flattening? | `core/business_zones.py` consumes archived `WeeklyProfile` snapshots; VPOC series available via `get_archived_profiles()` |
| Participant strength | Highs/lows are "weak" (retail, heavy volume at the extreme, auction unfinished — likely revisited) or "strong" (institutional, thin tail / fast rejection) | `core/profile_structure.classify_extremes()` |
| Double distribution | Two acceptance areas in one profile — market repriced mid-period | `core/profile_structure.detect_double_distribution()` (wraps `profile_shape.classify_shape` shape "B") |
| Single prints | Areas traversed so fast that almost no volume printed — market "owes" a revisit ("repair") | `core/profile_structure.find_single_prints()` |

**Note on the weak/strong heuristic (market-profile convention):** a *thin*
extreme (volume at the edge ≪ profile average) means fast institutional
rejection → **strong**. A *fat* extreme (volume at the edge comparable to the
body) means the auction ended without excess → **weak / poor high-low**,
statistically likely to be revisited.

## Step 6 — Business Zones (Smart Support & Resistance)

Zones where a reaction from big players is expected, derived from VAH / VAL /
POC / HVN / LVN / single prints of archived profiles.

| Concept | Definition | Module / Feature |
|---|---|---|
| Derived levels | VAH, VAL, POC, HVN of past profiles | `core/business_zones.build_zones()` |
| Zone strength | Zones confirmed by multiple profiles (recurrence) are stronger | `Zone.recurrence`, `Zone.strength` |
| Point A → Point B | The expected path: from one volume node to the next | `nearest_zones()` — nearest zone below / above current price |
| Repair areas | Old single prints the market may return to fill | `Zone(kind="single_print")` with `state` untested → repaired |

## Step 7 — Daily Road Map (Behavioral Expectations)

The strategic plan for how to interact with the zones.

| Concept | Definition | Module / Feature |
|---|---|---|
| Speed map | Fast movement expected through LVN / single prints; rotation inside HVN / POC | `core/road_map.build_road_map()` → `zone_expectations` |
| Day type | Trend vs. balance, dominant direction | `road_map["day_type"]` from session + weekly regime |
| Setup matrix | Which setups are allowed today (balance day → only counter-trades at zone edges; trend day → continuation/pullback) | `road_map["allowed_setups"]` |

## Step 8 — Setup Identification (The Proof)

Only after steps 5–7 do we look for a setup — the proof the idea is working.

| Concept | Definition | Module / Feature | Status |
|---|---|---|---|
| P/b/D/B structures | Profile shape confirms trend / balance / double distribution | `core/profile_shape.classify_shape()` | ✅ live |
| Absorption | Price stalls despite high volume | `core/absorption.py`, pattern engine flag | ✅ live |
| Delta divergence | Price extreme without CVD confirmation | `core/pattern_engine.detect_delta_divergence()` | ✅ live (Sprint B) |
| Lethargy | Market speed slowing at a zone | — | ❌ backlog |
| Candle proof at location | Candle pattern (e.g. inside bar) valid only when backed by volume / at a planned LVN | `core/candle_classifier.py` (NCI set); linkage to zones via `road_map` + `business_zones` | ⚠️ inside bar + linkage pending |

## Feature contract (what the Strategy Engine consumes)

Every Layer-3 module contributes flat, deterministic fields that can be
joined into the feature DataFrame (P3: reine Funktionen ueber Profildaten):

```
profile_structure:  n_single_prints, high_strength, low_strength,
                    is_double_distribution, dist_upper_poc, dist_lower_poc
business_zones:     zone_above_price / kind / strength / distance,
                    zone_below_price / kind / strength / distance,
                    n_unrepaired_single_prints
road_map:           day_type, dominant_direction, allowed_setups,
                    expected_speed_above, expected_speed_below
```

## Backlog (explicitly not built yet)

- Lethargy detector (trade-rate slowdown at zone touch)
- Inside/outside/engulfing bar in candle classifier (arch doc §4.4)
- Weekly VPOC trend series as an explicit feature (rising/falling/flat over N weeks)
- Signal Agent prompt: include road_map + zones (Sprint B spec item 4)
