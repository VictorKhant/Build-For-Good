"""Verification worksheet, ranked by what can actually change a demo result.

Two-leg model: what matters is the agencies that win collections and the
hubspots that receive food. Everything else can wait.

    python verify.py            # prioritised worksheet
    python verify.py --md       # markdown
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import agencies as ag_mod
import demand
import needs
import pipeline
import scenarios
from economics import CONFIG, CONFIG_SOURCES

DATA = Path(__file__).resolve().parent / "data"


def load():
    ags = ag_mod.load(CONFIG)
    dests = json.loads((DATA / "destinations.json").read_text())
    return ags, [d for d in dests if d["dest_type"] == "hubspot"]


def demo_impact(ags, hubs):
    a_hits, h_hits = Counter(), Counter()
    for name in scenarios.SCENARIOS:
        demand.LEDGER.reset()
        r = pipeline.run(scenarios.get(name)["donation"], ags, hubs,
                         now=scenarios.DEMO_NOW)
        for rank, m in enumerate(r["collection"]["matches"][:5]):
            a_hits[m["agency_id"]] += (5 - rank)
        l2 = r["distribution"]
        if l2 and l2.get("feasible"):
            for st in l2["stops"]:
                h_hits[st["dest_id"]] += 5
    demand.LEDGER.reset()
    return a_hits, h_hits


def report(md: bool = False) -> None:
    ags, hubs = load()
    a_hits, h_hits = demo_impact(ags, hubs)
    by_id = {a["agency_id"]: a for a in ags}
    budgets = ag_mod.intake_demand(ags, CONFIG)

    H = (lambda t: f"\n## {t}\n") if md else (lambda t: f"\n{'='*74}\n{t}\n{'='*74}")
    B = (lambda s: f"- {s}") if md else (lambda s: f"  {s}")

    print("# Seed verification worksheet" if md else "SEED VERIFICATION WORKSHEET")

    if ag_mod.is_simulated(ags):
        print()
        print("!! AGENCY DATA IS SIMULATED. Nothing in Tier 1 is a real organisation.")
        print("!! Replace data/agencies.json (see AGENCY_SCHEMA.md), then re-run this.")

    print(H("TIER 1 — agencies that win collections (verify all of these)"))
    print("These appear in the top 5 of at least one rehearsed scenario. If an hour")
    print("or a mobile-pantry flag here is wrong, the winning match is wrong.")
    print()
    for aid, _ in a_hits.most_common():
        a, b = by_id[aid], budgets[aid]
        kind = "MOBILE pantry" if a["has_mobile_pantry"] else "walk-in pantry"
        days = "".join("MTWTFSS"[w["dow"]] for w in a["operating_windows"]) or "none"
        print(B(f"[ ] {a['name']}   ({kind})"))
        print(f"      {a.get('address', '—')}")
        print(f"      collection hours: {days}")
        if a["has_mobile_pantry"]:
            md_ = "".join("MTWTFSS"[w["dow"]] for w in a["mobile_windows"]) or "none"
            print(f"      mobile runs: {md_}   capacity {a['mobile_capacity_lbs']:.0f} lb"
                  f"   up to {a['max_hubspot_stops']} stops")
            print(f"      CHECK: do they really run a mobile pantry? which days? "
                  f"does it reach downtown?")
        else:
            print(f"      walk-in demand: {b['demand_lbs']:.0f} lb/day from "
                  f"{b['n_blocks']} blocks ({b.get('walk_in_people', 0):.0f} people)")
            print(f"      CHECK: still operating? correct hours? do unsheltered "
                  f"people actually come here?")
        print()

    print(H("TIER 2 — hubspots receiving food"))
    print("Block-level outreach points. Derived from the count data, not entered by")
    print("hand, so the check is whether outreach actually happens at these blocks.")
    print()
    for hid, _ in h_hits.most_common():
        h = next(x for x in hubs if x["dest_id"] == hid)
        print(B(f"[ ] {h['name']}  —  {h['need_now']:.0f} people, "
                f"trend {h['need_trend']:+.0f}/yr, block {h.get('block_id')}"))
    if not h_hits:
        print(B("none received food in the rehearsed scenarios — check leg 2 timing"))

    print(H("TIER 3 — CONFIG constants (a judge will ask)"))
    unver = [(k, v) for k, v in CONFIG_SOURCES.items() if not v["verified"]]
    print(f"{len(unver)} of {len(CONFIG_SOURCES)} still need a human to confirm.")
    print()
    for k, v in unver:
        print(B(f"[ ] {k} = {v['value']}"))
        print(f"      {v['source']}")
        if v.get("note"):
            print(f"      {v['note']}")
        print()

    print(H("ALREADY MACHINE-CHECKED"))
    cache = json.loads((DATA / "geocode_cache.json").read_text())
    zipof = lambda s: (re.findall(r"\b(9\d{4})\b", s) or [None])[-1]
    issues = [k for k, v in cache.items() if v is None]
    drift = [(k, v["matched"]) for k, v in cache.items()
             if v and zipof(k) and zipof(v["matched"]) and zipof(k) != zipof(v["matched"])]
    print(B(f"geocoding: {len(issues)} failures, {len(drift)} zip drifts "
            f"across {len(cache)} addresses"))
    idx = needs.get_index()
    print(B(f"block grid: {len(idx.blocks)} polygons, "
            f"{len(idx.panel_block_ids)} in the comparable panel"))
    mob = [a for a in ags if a["has_mobile_pantry"]]
    print(B(f"agencies: {len(ags)} total, {len(mob)} with mobile pantries "
            f"(only these can serve a hubspot)"))
    zero = [a for a in ags if not a["has_mobile_pantry"]
            and budgets[a["agency_id"]]["demand_lbs"] <= 0]
    print(B(f"{len(zero)} fixed pantries have NO walk-in demand — they sit outside "
            f"the counted population and cannot win a match"))

    print(H("TIER 4 — safe to skip"))
    never = [a for a in ags if a["agency_id"] not in a_hits]
    print(B(f"{len(never)} agencies never appear in any rehearsed scenario."))
    print(B("sd_foodbank_sites.csv is no longer used as destinations. Those 70 sites "
            "are candidate AGENCIES — cross-check them against your teammate's roster."))


if __name__ == "__main__":
    report(md="--md" in sys.argv)
