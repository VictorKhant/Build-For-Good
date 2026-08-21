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
