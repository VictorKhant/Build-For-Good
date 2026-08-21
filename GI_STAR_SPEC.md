# Getis-Ord Gi\* — hotspot detection for BellyUp

*A build spec. Written to be implemented directly, or handed to a teammate.*

**Status: not built.** Nothing in this document exists in the codebase yet.

---

## 0. The problem it solves

Right now a hotspot is any block where `need >= 1.0`
(`CONSTANTS["MIN_CANDIDATE_NEED"]` in `bellyup/demo_data.py:100`). That threshold
was chosen, not derived. It has two failure modes, and both are visible in the
real data:

| Block | need | in the pool today? | should it be? |
|---|---|---|---|
| 16th St & K St | 4.5 | yes, barely | **yes** — 13 neighbouring blocks also have need |
| Park Bl & J St | 23.2 | yes, strongly | **doubtful** — an isolated spike |

A threshold can only see one block at a time. It cannot tell the difference
between *need in the middle of a dense band* and *need on a lone corner*.

That distinction is operationally real. A van parked in a cluster is within
walking distance of need across a dozen blocks; a van parked on a spike serves
that block and nothing else. Same drive, different reach.

Gi\* is the standard method for exactly this question, and it is what the
spatial-statistics literature would expect to see here.

### What it changes in the product

- `hotspot` stops meaning *"above a line I drew"* and starts meaning
  *"a statistically significant cluster, p < 0.05"*.
- The dispatcher gains a reason to prefer a cluster over an equally distant
  spike, alongside the access-gap boost it already applies.
- On the stage: **161 blocks pass the threshold; 54 are significant clusters.**
  That is a sharper claim than the one being made now.

---

## 1. The statistic

For each block *i*, compare the total need in *i and its neighbours* against
what you would expect if the same total need were scattered at random across
all blocks.

```
        Σⱼ wᵢⱼ xⱼ  −  X̄ Σⱼ wᵢⱼ
Gᵢ* = ─────────────────────────────────────────
                      ┌─────────────────────────
              S ·    │  n Σⱼ wᵢⱼ² − (Σⱼ wᵢⱼ)²
                    │  ────────────────────────
                   ╲│          n − 1
```

Where:

| symbol | meaning |
|---|---|
| `xⱼ` | need at block *j* |
| `wᵢⱼ` | 1 if *j* is a neighbour of *i* (or *i* itself), else 0 |
| `X̄` | mean need across all *n* blocks |
| `S` | population standard deviation of need |
| `n` | number of blocks (382) |

The result is a **z-score**. Because it is a z-score, the interpretation is the
usual one:

| z | reading |
|---|---|
| > 2.58 | cluster, p < 0.01 |
| > 1.96 | cluster, p < 0.05 |
| −1.96 … 1.96 | not distinguishable from random |
| < −1.96 | significant *cold* spot |

Note the `*` in Gi\*: the block includes **itself** in its own neighbourhood.
Plain Gi excludes it. Gi\* is the right one here — a block's own need obviously
counts toward whether that spot is worth visiting.

### Why the denominator is not just `S`

The variance term corrects for how many neighbours a block has. A block with 16
neighbours accumulates more total need than one with 4 simply by having more
terms in the sum. Without that correction, blocks in the dense middle of the
grid would score high purely for being central — the same failure mode that made
an 800 m service radius measure centrality instead of catchment
(see `README.md`, "Two radii").

---

## 2. Implementation

### 2.1 New file: `bellyup/spatial.py`

Pure `numpy` and `math`. No new dependencies.

```python
"""Getis-Ord Gi* — is the need at this block a real cluster, or a lone spike?

A threshold on a single block cannot tell those apart, and they call for
different operational answers: a cluster means one stop reaches a
neighbourhood, a spike means one stop reaches one corner.
"""

from __future__ import annotations

import math

# Neighbour radius. Downtown blocks run roughly 90x60 m, so 250 m reaches the
# ring of blocks immediately around one -- typically 8 to 16 of them.
#
# This constant carries the same warning as SERVICE_RADIUS_M: the answer moves
# with it. At 150 m most blocks have too few neighbours for the statistic to
# say anything; at 600 m a third of downtown is one another's neighbour and
# every z-score drifts toward zero. Sensitivity across the range belongs in
# the verification step below, not in a footnote.
NEIGHBOUR_M = 250.0


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Flat-earth distance. Fine over a 3 km grid; see needs.py for the same
    approximation and why it is safe at this scale."""
    return math.hypot((a[0] - b[0]) * 111_000.0, (a[1] - b[1]) * 94_000.0)


def gi_star(blocks: list[dict], radius_m: float = NEIGHBOUR_M) -> dict[str, dict]:
    """block_id -> {z, p_flag, n_neighbours, local_sum}.

    `blocks` needs `id`, `lat`, `lon`, `need`. Returns a z-score per block.
    """
    ids = [b["id"] for b in blocks]
    x = {b["id"]: float(b["need"]) for b in blocks}
    pos = {b["id"]: (b["lat"], b["lon"]) for b in blocks}

    n = len(ids)
    if n < 3:
        return {i: {"z": 0.0, "flag": "n/a", "n_neighbours": 0} for i in ids}

    mean = sum(x.values()) / n
    var = sum((v - mean) ** 2 for v in x.values()) / n
    sd = math.sqrt(var)

    out: dict[str, dict] = {}
    for i in ids:
        # w includes i itself -- this is Gi*, not Gi
        grp = [j for j in ids if j == i or _metres(pos[i], pos[j]) <= radius_m]
        k = len(grp)
        local = sum(x[j] for j in grp)

        expected = mean * k
        # Sum of w == k and sum of w^2 == k for binary weights
        denom_var = sd ** 2 * (n * k - k * k) / (n - 1)
        z = (local - expected) / math.sqrt(denom_var) if denom_var > 0 else 0.0

        out[i] = {
            "z": round(z, 3),
            "flag": ("hot99" if z > 2.58 else "hot95" if z > 1.96
                     else "cold95" if z < -1.96 else "none"),
            "n_neighbours": k - 1,
            "local_sum": round(local, 1),
        }
    return out
```

**Complexity note.** This is O(n²) — 382² is 146k distance calls, a few hundred
milliseconds. Do not optimise it. If the grid ever grows, cache the neighbour
lists the way `needs.NeedIndex.distance_profile` already does.

### 2.2 Attach the scores at load

`bellyup/demo_data.py`, in `load_hotspots()` (need enters at line 154):

```python
def load_hotspots() -> list[dict]:
    out = []
    for r in _rows("hotspots.csv"):
        ...                                     # unchanged
    out.sort(key=lambda h: -h["need"])

    # Gi* over the full grid, not just the blocks above the cut -- a block with
    # zero need is still evidence about its neighbours' significance, and
    # dropping it first would bias every z-score upward.
    import spatial
    gi = spatial.gi_star(out)
    for h in out:
        s = gi[h["id"]]
        h["giZ"] = s["z"]
        h["giFlag"] = s["flag"]
        h["giNeighbours"] = s["n_neighbours"]
    return out
```

The comment matters: run Gi\* **before** any filtering. Computing it on the 161
blocks that already pass the threshold would be conditioning on the outcome.

### 2.3 New constants

`bellyup/demo_data.py`, in `CONSTANTS`:

```python
    "GI_Z_THRESHOLD": 1.96,     # p < 0.05, two-tailed
    "CLUSTER_BOOST_MAX": 0.25,  # reward premium for a significant cluster
```

`CLUSTER_BOOST_MAX` is a policy weight, not a measurement — the same category as
`ACCESS_BOOST_MAX`. Say so in `CONFIG_SOURCES` when you add it.

---

## 3. Wiring it into decisions

There are three places it could bite. **Do them in this order and stop when the
demo tells a clear story** — each one is a bigger behavioural change than the
last.

### 3.1 Map colour (safe, zero risk to matching)

`bellyup/static/board/app.js`, `hotspotStyle()` — currently sizes by
`sqrt(need)` and nothing else. Add a ring for significance:

```js
function hotspotStyle(h) {
  const closed = hotspotClosed(h);
  const col = closed ? themeColor("--c-route") : themeColor("--c-hotspot");
  const cluster = h.giFlag === "hot95" || h.giFlag === "hot99";
  return {
    radius: 3 + Math.sqrt(h.need) * 2.1,
    color: col,
    weight: cluster ? 2.6 : 1.4,          // clusters get a heavier ring
    opacity: closed ? 0.9 : (cluster ? 0.95 : 0.6),
    fillColor: col,
    fillOpacity: closed ? 0.3 : 0.16 + Math.min(h.need / 40, 0.34),
  };
}
```

Then say it in the tooltip (`hotspotTip()`), because an unexplained visual
difference is worse than none:

```js
  + (h.giFlag === "hot99" ? `<br><b>significant cluster</b> (p&lt;0.01, z=${h.giZ})`
   : h.giFlag === "hot95" ? `<br><b>significant cluster</b> (p&lt;0.05, z=${h.giZ})`
   : h.need >= 1 ? `<br>isolated need — not a statistical cluster` : "")
```

Add a legend row in `index.html` next to the existing hotspot rows.

**This alone may be enough for the demo.** It makes the claim visible without
touching a single dispatch decision.

### 3.2 Reward premium (changes rankings)

`bellyup/dispatch.py:403` currently computes:

```python
boost = 1 + C["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7
```

Add a second, multiplicative term:

```python
            access = 1 + C["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7
            # A stop in a cluster is within reach of need across the
            # surrounding blocks; a stop on an isolated spike reaches that
            # block only. Same drive, different reach.
            cluster = (1 + C["CLUSTER_BOOST_MAX"]
                       if h.get("giZ", 0) > C["GI_Z_THRESHOLD"] else 1.0)
            boost = access * cluster
```

There are **three** call sites with this formula: `dispatch.py` lines 403, 677
and 715 (the last inside `combine_run`). All three must change together or a
combined run will score differently from the single dispatch that fed it.

> Better: extract it into one function and call that from all three. The
> duplication is pre-existing and this is the moment it starts to hurt.

### 3.3 Candidate filter (biggest change — think before doing this)

Replacing `need >= MIN_CANDIDATE_NEED` with `giZ > 1.96` would cut the candidate
pool from 161 blocks to 54.

**I would not do this.** Two reasons:

1. A block with real counted people would become unservable because its
   neighbours are empty. Park Bl & J St has **23.2 person-equivalents** and
   z = 1.72. Refusing to feed 23 people because the statistic calls them
   isolated is the wrong answer, and it is the kind of thing a judge will
   catch.
2. Significance is a statement about spatial pattern, not about need. It should
   inform *preference*, not *eligibility*.

Keep the need threshold as the eligibility rule. Use Gi\* to rank within it.

---

## 4. Verification

Do all of this before believing any of it.

### 4.1 Reproduce the reference numbers

At `NEIGHBOUR_M = 250`, on the current `dataset/hotspots.csv`:

```
382 blocks | mean need 2.58 | sd 5.75
161 blocks pass need >= 1
 54 significant clusters (z > 1.96)
114 of the 161 are isolated, not clustered

top clusters:
  z=7.58  need  4.5  13 neighbours  16TH ST & K ST
  z=7.27  need 13.3  13 neighbours  16TH ST & J ST
  z=6.98  need  5.9  12 neighbours  16TH ST & IMPERIAL AV

high need but NOT significant:
  z=1.72  need 23.2  13 neighbours  PARK BL & J ST
  z=1.18  need 21.4  16 neighbours  03RD AV & A ST
```

If your implementation does not reproduce these, the weights or the variance
term are wrong. The most common mistake is excluding the block from its own
neighbourhood — that gives Gi, not Gi\*, and every number shifts.

### 4.2 Sanity checks

```python
# z-scores should be roughly centred and mostly unremarkable
zs = [s["z"] for s in gi.values()]
assert -6 < min(zs) and max(zs) < 12          # no absurd outliers
assert abs(sum(zs) / len(zs)) < 1.0            # roughly centred
assert sum(1 for z in zs if z > 1.96) < len(zs) * 0.25   # not "everything"

# a block with zero need and zero-need neighbours must not be a hot spot
# a block in the dense East Village band must be
```

### 4.3 Sensitivity — this is the honest part

Run the count of significant clusters at 150, 250, 400 and 600 m. **Put the
table in the README.** If the answer swings wildly, say so; the 300 m vs 800 m
radius finding already in the README is precedent for treating this openly
rather than picking the flattering number.

### 4.4 Regression

The existing suite must still pass unchanged:

```bash
cd bellyup && ../.venv/bin/python -m uvicorn app:app --port 8000
# then the 29-check regression used throughout this project
```

Specifically confirm the four rehearsed report quantities are untouched —
Scripps 129, Northgate 332, Hyatt 288, SpringHill 50 — since 3.2 changes
rankings and could silently reshuffle which collector wins.

---

## 5. What to claim, and what not to

### Fair to say

- "Hotspots are statistically significant clusters at p < 0.05, by Getis-Ord
  Gi\* — not blocks above a threshold we chose."
- "161 blocks have counted need; 54 of them are real clusters. That difference
  is the difference between a stop that reaches a neighbourhood and one that
  reaches a corner."
- "16th & K St has a quarter of Park Bl & J St's need and five times the
  significance, because it sits inside a dense band."

### Not fair to say

- ~~"Statistically validated need."~~ Gi\* tests spatial pattern. It says
  nothing about whether the count is accurate.
- ~~"Predicts where need will be."~~ It is descriptive, on one snapshot.
- ~~"p < 0.05 means we are 95% confident there is need here."~~ It means a
  cluster this concentrated would be unlikely under spatial randomness.

### Caveats to volunteer, unprompted

1. **12 observations per block.** The DSDP panel has 12 count dates. Everything
   downstream inherits that thinness.
2. **The neighbour radius is a choice.** 250 m; the answer moves with it, and
   §4.3 shows how much.
3. **Spatial randomness is the wrong null.** Downtown need is not randomly
   scattered — it is shaped by shelters, transit and enforcement. Gi\* asks
   "unlikely under randomness", which is a weaker question than "unlikely given
   how a city works".
4. **The count is a monthly visual sweep**, a known undercount that measures
   visibility as much as prevalence. Already on the limitations slide; it
   applies here too.

---

## 6. Effort

| step | time | risk |
|---|---|---|
| 2.1 `spatial.py` + reproduce §4.1 | 25 min | low |
| 2.2 attach at load | 5 min | low |
| 3.1 map colour + tooltip + legend | 20 min | none to matching |
| 4.3 sensitivity table | 10 min | none |
| 3.2 reward premium (3 sites) | 20 min | **changes rankings — regress** |

About 45 minutes to something demonstrable (through 3.1), 80 to something that
changes decisions.

**If you have less than an hour, do 2.1 through 3.1 and stop.** The visual claim
plus the 4.5-versus-23.2 contrast is most of the value, and none of it can break
a dispatch.

---

## 7. Files touched

```
new     bellyup/spatial.py
edit    bellyup/demo_data.py         load_hotspots(), CONSTANTS
edit    bellyup/static/board/app.js  hotspotStyle(), hotspotTip()
edit    bellyup/static/board/index.html   legend row
edit    bellyup/dispatch.py          only if doing 3.2 — three call sites
edit    README.md                    the claim, and the sensitivity table
```

No new dependencies. No dataset changes — `hotspots.csv` is untouched, the
scores are derived at load.
