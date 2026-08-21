"""
The three datasets the product needs, and nothing else.

  hotspots.csv   — where unsheltered people are          (EVENT DATA)
  businesses.csv — small businesses with surplus food    (EXTERNAL)
  agencies.csv   — who collects and redistributes        (EXTERNAL)
  mobile_pantries.csv — agency distribution points       (EXTERNAL, pending pull)

No routing. The connection model is:
  business -> agency -> hotspot
"""
import pandas as pd, numpy as np, math
from pathlib import Path

ROOT = Path("/root/project")
R, I, O = ROOT/"data/raw/dsdp_official", ROOT/"data/interim", ROOT/"outputs/final"
O.mkdir(parents=True, exist_ok=True)

# ============================================================ 1. HOTSPOTS
grid  = pd.read_csv(R/"Downtown_BlockGrid.csv")
panel = pd.read_csv(R/"BlockLevel_Counts_Panel261.csv", parse_dates=["report_month"])
full  = pd.read_csv(R/"BlockLevel_Counts.csv", parse_dates=["report_month"])
meth  = pd.read_csv(R/"Methodology_Periods.csv", parse_dates=["effective_from","effective_to"])

def mult(d):
    for _, m in meth.iterrows():
        if d >= m.effective_from and (pd.isna(m.effective_to) or d <= m.effective_to):
            return m.tent_multiplier, m.vehicle_multiplier
    return np.nan, np.nan

def persons(df):
    tm, vm = zip(*df.report_month.apply(mult))
    return (df.individuals.fillna(0) + np.array(tm)*df.tents_structures.fillna(0)
            + np.array(vm)*df.vehicles.fillna(0))

panel["persons"] = persons(panel)
agg = (panel.groupby("block_id")
       .agg(months_observed=("persons","size"),
            months_nonzero=("persons", lambda s:int((s>0).sum())),
            avg_persons=("persons","mean"),
            peak_persons=("persons","max"))
       .reset_index())

latest_m = full.report_month.max()
lat = full[full.report_month == latest_m].copy()
lat["persons"] = persons(lat)
lat = lat[["block_id","individuals","tents_structures","vehicles","persons"]].rename(
    columns={"individuals":"latest_individuals","tents_structures":"latest_tents",
             "vehicles":"latest_vehicles","persons":"latest_persons"})

h = (grid[["block_id","area","lon","lat","st_east","st_north"]]
     .merge(agg, on="block_id", how="left")
     .merge(lat, on="block_id", how="left"))
h["location"] = h.st_east.str.replace("_"," ") + " & " + h.st_north.str.replace("_"," ")
h["persistence"] = (h.months_nonzero/h.months_observed).round(3)
h["need"] = h.avg_persons.fillna(h.latest_persons).fillna(0).round(2)
h["need_rank"] = h.need.rank(ascending=False, method="min").astype(int)
h["priority"] = pd.cut(h.need, [-0.01,0.5,5,15,1e6],
                       labels=["none","low","medium","high"]).astype(str)
h["longitudinal_data"] = h.months_observed.notna()
h = h.sort_values("need", ascending=False)
h[["block_id","location","area","lon","lat","need","need_rank","priority",
   "persistence","months_nonzero","months_observed","avg_persons","peak_persons",
   "latest_persons","latest_individuals","latest_tents","latest_vehicles",
   "longitudinal_data"]].to_csv(O/"hotspots.csv", index=False)

# ============================================================ 2. BUSINESSES
d = pd.read_csv(I/"sb1383_food_donors.csv")
d = d[d.tier_qualifying.astype(str) == "True"].rename(
    columns={"facility_name":"business_name","approx_lon":"lon","approx_lat":"lat"})
rp = I/"restaurants.csv"
if rp.exists():
    r = pd.read_csv(rp)
    r["business_name"] = r["name"]; r["facility_type"] = "restaurant"
    r["sb1383_tier"] = 2; r["size_metric"] = r.get("seats", "")
    d = pd.concat([d, r], ignore_index=True)
b = d[["business_name","facility_type","address","lon","lat","sb1383_tier",
       "size_metric","source_url"]].copy()
b["surplus_type"] = np.where(b.facility_type.isin(["hotel","restaurant","venue"]),
                             "prepared", "packaged/produce")
b = b[b.lon.notna()].sort_values(["surplus_type","business_name"])
b.to_csv(O/"businesses.csv", index=False)

# ============================================================ 3. AGENCIES
a = pd.read_csv(I/"agencies.csv")
a.to_csv(O/"agencies.csv", index=False)

# ==================================================== 4. MOBILE PANTRIES
mp = I/"mobile_pantries.csv"
cols = ["site_name","operator","address","zip","lon","lat","geocode_method",
        "days_per_week","day_list","hours_per_visit","start_time","end_time",
        "program","eligibility","source_url"]
(pd.read_csv(mp) if mp.exists() else pd.DataFrame(columns=cols)).to_csv(
    O/"mobile_pantries.csv", index=False)

print(f"hotspots.csv        {len(h):>4} blocks  ({int((h.need>0).sum())} with need, "
      f"{int((h.priority=='high').sum())} high priority)")
print(f"businesses.csv      {len(b):>4} businesses ({int((b.surplus_type=='prepared').sum())} prepared)")
print(f"agencies.csv        {len(a):>4} operators")
print(f"mobile_pantries.csv {len(pd.read_csv(O/'mobile_pantries.csv')):>4} sites  <- NEEDS PULL")
print("\nTop 10 hotspots:")
print(h.head(10)[["need_rank","location","area","need","persistence","priority"]]
      .to_string(index=False, float_format=lambda v: f"{v:.2f}"))
