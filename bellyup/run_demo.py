"""Console harness -- the whole two-leg engine with no UI in front of it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import agencies as ag_mod
import demand
import pipeline
import scenarios
from economics import CONFIG

DATA = Path(__file__).resolve().parent / "data"


def load(cfg):
    ags = ag_mod.load(cfg)
    dests = json.loads((DATA / "destinations.json").read_text())
    hubs = [d for d in dests if d["dest_type"] == "hubspot"]
    return ags, hubs


def banner(ags):
    if ag_mod.is_simulated(ags):
        print("!" * 78)
        print("!  SIMULATED AGENCY DATA -- every agency below is invented.")
        print("!  Replace data/agencies.json before presenting. See AGENCY_SCHEMA.md.")
        print("!" * 78)
        print()


def show(name: str, cfg: dict | None = None) -> dict:
    c = cfg or CONFIG
    ags, hubs = load(c)
    demand.LEDGER.reset()
    s = scenarios.get(name)
    d = s["donation"]

    print("=" * 78)
    print(f"{s['label']}   [{d['donor_name']}]")
    print(f"  {d['quantity_lbs']:.0f} lb {d['food_type']} ({d['condition']}), "
          f"ready {d['ready_at']:%a %H:%M}, expires {d['expires_at']:%a %H:%M}")
    print("=" * 78)

    r = pipeline.run(d, ags, hubs, now=scenarios.DEMO_NOW, cfg=c)
    l1, l2 = r["collection"], r["distribution"]

    print(f"\n-- LEG 1  collection --  {l1['evaluated']} agencies screened, "
          f"{l1['feasible']} can collect")
    t = r["tax"]
    print(f"   donor incentive: est. ${t['deduction_estimate']:.2f} deduction "
          f"({t['rule']}) — estimate only")
    print()
    if l1["matches"]:
        for i, m in enumerate(l1["matches"][:4], 1):
            flag = "MOBILE" if m["serves_hubspots"] else "walk-in"
            print(f"   #{i} {m['agency_name'][:32]:34} [{flag:7}] "
                  f"takes {m['accepts_lbs']:5.0f} lb  {m['round_trip_km']:5.1f} km  "
                  f"${m['transport_cost']:6.2f}  ${m['cost_per_meal']:.2f}/meal  "
                  f"net ${m['net_value']:.2f}")
    else:
        print(f"   no agency can collect — {l1['headline']}")
    print()
    print("   rejections: " + ", ".join(
        f"{g['reason_code']}({g['count']})" for g in l1["rejection_summary"][:5]))

    print("\n-- LEG 2  distribution --")
    if l2 is None:
        w = l1["matches"][0] if l1["matches"] else None
        if w:
            print(f"   {w['agency_name']} is a walk-in pantry — the journey ends there, "
                  f"people come to it.")
        else:
            print("   n/a")
    elif l2["feasible"]:
        print(f"   {l2['agency_name']} mobile pantry: {l2['n_stops']} stop(s), "
              f"{l2['delivered_lbs']:.0f} lb, {l2['route_km']['total']:.1f} km, "
              f"${l2['cost_per_meal']:.2f}/meal, departs {l2['departs_at'][-5:]}")
        for st in l2["stops"]:
            nm = st["name"].replace("Outreach hubspot — ", "")
            print(f"       {st['lbs']:5.0f} lb -> {nm[:26]:28} need {st['need_now']:4.0f} "
                  f"trend {st['need_trend']:+5.0f}  budget {st['daily_demand_lbs']:4.0f} lb")
    else:
        print(f"   not possible now — {l2['reason']}")

    print(f"\n   OUTCOME: {r['outcome']['summary']}")
    if l1["matches"]:
        print(f"\n   {l1['matches'][0]['explanation']}")
    if l2 and l2.get("feasible"):
        print(f"\n   {l2['explanation']}")
    print()
    return r


if __name__ == "__main__":
    ags, _ = load(CONFIG)
    banner(ags)
    for n in (sys.argv[1:] or list(scenarios.SCENARIOS)):
        show(n)
