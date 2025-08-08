"""
North-American Rail, Major Airports & Population-Weighted HSR Corridors - Folium
-------------------------------------------------------------------------------
* Freight rail     → brown                * Amtrak stations  → blue
* Amtrak routes    → green                * Major airports   → purple
* Corridors scored popA x popB / dist²    * 100-500-mile links
Outputs:
  1) Interactive HTML map (north_america_rail_air_metro_corridors_pop_weighted_final.html)
  2) corridors_top100.geojson (top-100 great-circle LineStrings)
  3) corridor_output_log.txt  (verbose per-corridor log, incl. anchor breakdown)
"""

import itertools, re, textwrap
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import folium, branca
from folium.plugins import MarkerCluster
from shapely.geometry import LineString, Point
from pyproj import Geod

# ───────────── 0. FILE PATHS ───────────────────────────────────────────
DATA = Path("/Data")
OUTPUT = Path("/Output")
freight_path  = DATA / "NTAD_North_American_Rail_Network_Lines_4887242439196784421/North_American_Rail_Network_Lines.shp"
amtrak_path   = DATA / "NTAD_Amtrak_Routes_7440257972717285207/Amtrak_Routes.shp"
stations_path = DATA / "NTAD_Amtrak_Stations_-6442145990651971831/Amtrak_Stations.shp"
states_path   = DATA / "tl_2023_us_state/tl_2023_us_state.shp"              # set None to hide
airports_path = DATA / "NTAD_Aviation_Facilities_6916451147882473169/Aviation_Facilities.shp"
cbsa_path     = DATA / "tl_2023_us_cbsa/tl_2023_us_cbsa.shp"
place_path    = DATA / "places_usa_2023.gpkg"                               # TIGER/Line Places GPKG

MIN_MI, MAX_MI = 100, 500
TOP_N          = 100
log_path       = OUTPUT / "corridor_output_log.txt"                           # detailed corridor log

# ───────────── Misc. helpers ───────────────────────────────────────────
def norm(name: str) -> str:
    """Lower‑case & strip Census suffixes like ‘ city’, ‘ town’, ‘ village’, ‘ CDP’."""
    name = name.lower().strip()
    return re.sub(r"\s+(city|town|village|c?dp)$", "", name)

geod = Geod(ellps="WGS84")
def miles(p1: Point, p2: Point) -> float:
    _, _, d = geod.inv(p1.x, p1.y, p2.x, p2.y)
    return d / 1609.344

def gc_line(p1: Point, p2: Point, n=20) -> LineString:
    pts = geod.npts(p1.x, p1.y, p2.x, p2.y, n - 2)
    return LineString([(p1.x, p1.y), *pts, (p2.x, p2.y)])

# ───────────── 1. LOAD CORE LAYERS ─────────────────────────────────────
print("Loading datasets …")
freight  = gpd.read_file(freight_path).to_crs(4326)[["geometry"]]
amtrak   = gpd.read_file(amtrak_path ).to_crs(4326)[["geometry"]]
stations = gpd.read_file(stations_path).to_crs(4326)
air      = gpd.read_file(airports_path).to_crs(4326)
states   = (gpd.read_file(states_path).to_crs(4326)[["geometry", "STUSPS"]]
            if states_path else None)

# ───────────── 2. FILTER MAJOR AIRPORTS (simple heuristic) ────────────
def is_major_airport(row):
    cert = str(row.get("FAR_139_TY", "")).strip().upper()
    code = str(row.get("ARPT_ID", "")).strip()
    return cert.startswith("I") and len(code) == 3 and code.isalpha()

air = air[air.apply(is_major_airport, axis=1)].copy()
air["geometry"] = air.geometry.simplify(0.001)
print(f"  ✔ retained {len(air)} Class‑I airports")

# ───────── 3. PRINCIPAL‑CITY WEIGHTED CBSA ANCHORS ────────────────────
print("Computing principal‑city weighted anchors …")

# 3‑A  TIGER/Line Places → centroids
places_ll   = gpd.read_file(place_path, layer="places").to_crs(4326)
places_proj = places_ll.to_crs("EPSG:5070")
places_ll["centroid"] = places_proj.centroid.to_crs(4326)
places_ll["key"]      = places_ll["NAME"].apply(norm) + "|" + places_ll["STATEFP"]
place_lookup = dict(zip(places_ll["key"], places_ll["centroid"]))

# 3‑B  ACS 2023 place‑level population
acs_place = requests.get(
    "https://api.census.gov/data/2023/acs/acs5",
    params={"get": "NAME,B01001_001E", "for": "place:*"}
).json()
pop_place = pd.DataFrame(acs_place[1:], columns=["CENSUS_NAME", "POP", "STATE", "PLACEFP"])
pop_place["POP"]   = pd.to_numeric(pop_place["POP"], errors="coerce")
pop_place["CITY"]  = pop_place["CENSUS_NAME"].str.replace(r",.*$", "", regex=True).apply(norm)
pop_place["key"]   = pop_place["CITY"] + "|" + pop_place["STATE"]
pop_lookup = pop_place.set_index("key")["POP"].to_dict()

# 3‑C  State abbrev. → FIPS
state_abbr_to_fips = {
 'AL':'01','AK':'02','AZ':'04','AR':'05','CA':'06','CO':'08','CT':'09','DE':'10',
 'DC':'11','FL':'12','GA':'13','HI':'15','ID':'16','IL':'17','IN':'18','IA':'19',
 'KS':'20','KY':'21','LA':'22','ME':'23','MD':'24','MA':'25','MI':'26','MN':'27',
 'MS':'28','MO':'29','MT':'30','NE':'31','NV':'32','NH':'33','NJ':'34','NM':'35',
 'NY':'36','NC':'37','ND':'38','OH':'39','OK':'40','OR':'41','PA':'42','RI':'44',
 'SC':'45','SD':'46','TN':'47','TX':'48','UT':'49','VT':'50','VA':'51','WA':'53',
 'WV':'54','WI':'55','WY':'56','PR':'72','VI':'78','GU':'66','AS':'60','MP':'69'
}

def anchor_from_principal_cities(name_field, poly_geom):
    """
    Given CBSA NAME ('Chicago-Naperville-Elgin, IL-IN-WI'), return
    a population‑weighted anchor Point plus a debug list:
      (anchor_pt, [(city, ST, lat, lon, pop), …])
    """
    name_part, state_part = name_field.split(",", 1)
    cities  = [norm(c) for c in name_part.split("-")]
    states  = [s.strip() for s in state_part.split()[0].split("-")]

    pts, wts, dbg = [], [], []
    for city in cities:
        for st_abbr in states:
            fips = state_abbr_to_fips.get(st_abbr)
            if not fips:
                continue
            key = f"{city}|{fips}"
            if key in place_lookup:
                pt  = place_lookup[key]
                pop = pop_lookup.get(key, 1)
                pts.append(pt);  wts.append(pop)
                dbg.append((city.title(), st_abbr, pt.y, pt.x, None if pop == 1 else pop))
                break  # only keep first matching state for city

    if pts:
        tot    = sum(wts)
        anchor = Point(sum(p.x * w for p, w in zip(pts, wts)) / tot,
                       sum(p.y * w for p, w in zip(pts, wts)) / tot)
    else:
        anchor, dbg = poly_geom.representative_point(), []

    return anchor, dbg

# 3‑D  Apply to every CBSA
cbsa_raw = gpd.read_file(cbsa_path).to_crs(4326)[["GEOID", "NAME", "geometry"]]
anchors, anchor_debugs = [], []
for _, rec in cbsa_raw.iterrows():
    pt, dbg = anchor_from_principal_cities(rec.NAME, rec.geometry)
    anchors.append(pt);  anchor_debugs.append(dbg)

cbsa = cbsa_raw.copy()
cbsa["anchor"]       = anchors
cbsa["anchor_debug"] = anchor_debugs
cbsa = cbsa.set_geometry("anchor")

# ───────────── 4. CBSA POPULATION (for scoring) ───────────────────────
acs_cbsa = requests.get(
    "https://api.census.gov/data/2023/acs/acs1",
    params={
        "get": "NAME,B01001_001E",
        "for": "metropolitan statistical area/micropolitan statistical area:*"
    }
).json()
pop_cbsa = pd.DataFrame(acs_cbsa[1:], columns=["NAME", "POP", "GEOID"])
pop_cbsa["POP"] = pd.to_numeric(pop_cbsa["POP"], errors="coerce")
cbsa = cbsa.merge(pop_cbsa[["GEOID", "POP"]], on="GEOID", how="left")

# ───────────── 5. SCORE & LOG EVERY ELIGIBLE PAIR ─────────────────────
print("Scoring corridors …")
pairs, corridor_no = [], 1
with open(log_path, "w", encoding="utf-8") as log:
    for (_, a), (_, b) in itertools.combinations(cbsa.iterrows(), 2):
        if pd.isna(a.POP) or pd.isna(b.POP):
            continue
        d = miles(a.anchor, b.anchor)
        if not (MIN_MI <= d <= MAX_MI):
            continue

        score = (a.POP * b.POP) / d**2
        pairs.append({
            "from":     a.NAME.split(",")[0],
            "to":       b.NAME.split(",")[0],
            "score":    score,
            "geometry": gc_line(a.anchor, b.anchor)
        })

        # ── verbose corridor log ──────────────────────────────────────
        log.write(f"{corridor_no}. Corridor: {a.NAME} ↔ {b.NAME}\n")
        log.write(f"   From: {a.anchor.y:.4f}, {a.anchor.x:.4f}  |  "
                  f"To: {b.anchor.y:.4f}, {b.anchor.x:.4f}\n")
        log.write(f"   Distance: {d:.1f} mi  |  Score: {score:,.0f}\n")
        for side, rec in (("From", a), ("To", b)):
            log.write(f"   Cities ({side}: {rec.NAME}):\n")
            for city, st, lat, lon, pop in rec.anchor_debug:
                pop_txt = f"{pop:,}" if pop else "N/A"
                log.write(f"      - {city}, {st} → ({lat:.4f}, {lon:.4f})  Pop: {pop_txt}\n")
        log.write("\n")
        corridor_no += 1

print(f"  ✔ corridors computed & logged ({corridor_no - 1})")

corridors = (
    gpd.GeoDataFrame(pairs, crs=4326)
      .sort_values("score", ascending=False)
      .head(TOP_N)
)
corridors.to_file(OUTPUT / "corridors_top100.geojson", driver="GeoJSON")
print("  ✔ top‑100 corridors saved to corridors_top100.geojson")

# ───────────── 6. DRAW MAP ────────────────────────────────────────────
print("Rendering Folium map …")
max_s = corridors["score"].max()
m = folium.Map(location=[39.5, -98.35], zoom_start=4,
               tiles="CartoDB Positron", prefer_canvas=True)

if states is not None:
    folium.GeoJson(states, name="States",
                   style_function=lambda _:{
                       "color": "black", "weight": 1, "fillOpacity": 0
                   }).add_to(m)

folium.GeoJson(freight, name="Freight Rail",
               style_function=lambda _:{
                   "color": "#8B4513", "weight": 1, "opacity": 0.7
               }).add_to(m)

folium.GeoJson(amtrak, name="Amtrak Routes",
               style_function=lambda _:{
                   "color": "#008000", "weight": 2, "opacity": 0.9
               }).add_to(m)

# stations & airports
clu = MarkerCluster(name="Amtrak Stations").add_to(m)
name_col = next((c for c in ["StationNam", "Name", "NAME", "STATION"] if c in stations), None)
for _, r in stations.iterrows():
    folium.Marker(
        [r.geometry.y, r.geometry.x],
        popup=r.get(name_col, "Station"),
        icon=folium.Icon(color="blue", icon="train", prefix="fa")
    ).add_to(clu)

air_fg = folium.FeatureGroup(name="Airports (Class‑I)")
for _, r in air.iterrows():
    folium.CircleMarker(
        [r.geometry.y, r.geometry.x],
        radius=6, color="purple", fill=True, fill_opacity=0.6,
        popup=r.get("ARPT_NAME", "Airport")
    ).add_to(air_fg)
air_fg.add_to(m)

cmap = branca.colormap.LinearColormap(["yellow", "orange", "red"], vmin=0, vmax=max_s)
folium.GeoJson(
    corridors, name=f"Top {TOP_N} Corridors",
    style_function=lambda f:{
        "color": cmap(f["properties"]["score"]),
        "weight": 1 + 6 * (f["properties"]["score"] / max_s),
        "opacity": 0.8
    },
    tooltip=folium.GeoJsonTooltip(fields=["from", "to"], aliases=["From", "To"])
).add_to(m)
cmap.caption = "Corridor Score"; cmap.add_to(m)

# custom legend block in bottom‑right corner
m.get_root().html.add_child(folium.Element(textwrap.dedent("""
    <div style="position:fixed; bottom:10px; right:10px; z-index:9999;
                font-size:13px; background:white; border:2px solid #444;
                border-radius:4px; padding:6px 10px; box-shadow:0 0 8px rgba(0,0,0,0.3);">
      <b>Layer Legend</b><br>
      <span style="border-bottom:4px solid #8B4513;">&nbsp;&nbsp;&nbsp;</span>&nbsp;Freight Rail<br>
      <span style="border-bottom:4px solid #008000;">&nbsp;&nbsp;&nbsp;</span>&nbsp;Amtrak Routes<br>
      <span style="background:purple; width:10px; height:10px; display:inline-block; border-radius:50%;"></span>&nbsp;Class‑I Airport<br>
      <span style="background:blue; width:10px; height:10px; display:inline-block; border-radius:50%;"></span>&nbsp;Amtrak Station
    </div>
""")))

folium.LayerControl(collapsed=False).add_to(m)
html_out = OUTPUT / "north_america_rail_air_metro_corridors_pop_weighted_final.html"
m.save(html_out)

print("✅  Map written to:", html_out)
print("📝  Corridor log saved to:", log_path)
