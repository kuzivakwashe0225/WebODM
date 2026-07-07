# Precise Agric — Plain-language guide to the numbers

A cheat-sheet for demos: what each value means for a farmer, and a simple line you can
actually say out loud. None of this requires technical knowledge to explain.

> **Golden rule for demos:** the *direction over time* and the *map (heatmap/zones)* tell the
> story better than any single raw number. Show a farmer the red vs green patches and whether
> the line is going up week to week — that lands far better than reading a decimal.

---

## Plant Health

The headline "how healthy / how green is the crop" score. Two possible versions depending on
the camera used:

### NDVI (when a near-infrared / multispectral camera is used)
A score from **−1 to +1**. Think of it as a greenness & vigour score:

| NDVI value | What it means | Say to a farmer |
|---|---|---|
| below 0.2 | bare soil, water, or dead/very stressed plants | "Almost no healthy crop here yet." |
| 0.2 – 0.5 | moderate / still developing crop | "The crop is growing but not fully established." |
| above 0.5 | dense, healthy, vigorous crop | "Strong, healthy growth here." |

The closer to **1**, the healthier and thicker the crop.

### EXG — "Excess Green" (when only a normal colour/RGB camera is used)
A **greenness score built from red, green and blue**. **It is NOT a 0–100 percentage** — it can
even be **negative**, and that is normal. What matters is: *higher = more lush green vegetation;
near zero or negative = very little green cover* (bare soil, dry/dead material, or a thin, sparse
crop).

**Example — "−2.58 EXG":** this field is showing **very little green vegetation** on that date —
mostly bare ground or stressed/sparse crop. As the crop grows and greens up, this number climbs.

> Say to a farmer: *"This is a greenness score. Right now it's low, meaning the field is still mostly
> bare soil. Watch it rise over the coming weeks as your crop fills in — and use the red/green map to
> see exactly which parts are lagging."*

Because EXG isn't a fixed scale, **compare fields to each other and track the trend over the season**
rather than reading the raw number alone.

---

## Canopy Cover %  (e.g. "26.11%")
The **percentage of the ground covered by green crop**, seen from above.

- 26% ≈ about a quarter of the field is covered by plants; the rest is still bare soil or gaps.
- Early season = low; it grows as plants spread; near 100% = the crop has fully "closed" over the soil.

> Say to a farmer: *"About a quarter of this field is covered by crop so far. We'll watch this climb —
> if one field stalls while others fill in, that's an early warning to check it."*

Useful for: judging establishment, spotting thin/patchy areas, and forecasting canopy closure & harvest timing.

---

## Weed Hotspots  (e.g. "43 hotspots")
The number of **distinct spots where the system flagged likely weeds**.

- 43 hotspots = 43 areas worth scouting/spraying. **Fewer is better.**
- Rising week to week = weeds are spreading → act sooner.
- It's an **automated guide for where to look**, not an exact weed count.

> Say to a farmer: *"We found 43 likely weed patches. Instead of walking the whole field, go straight
> to these spots. If this number climbs next week, it's time to spray."*

---

## VARI — Visible Vigour  (roughly −1 to +1)
A **greenness/vigour score from an ordinary colour camera**. Higher = healthier, greener foliage.
Handy when there's no special (infrared) camera. Rising VARI forecasts a stronger canopy ahead.

## GLI — Green Leaf Index  (roughly −1 to +1)
A **leaf-greenness / chlorophyll indicator**. Higher = greener, actively-growing leaves. A drop can
hint at ageing leaves or a nitrogen shortage before you'd notice it by eye.

---

## The pictures (often the best part of a demo)

- **Heatmap** — a **red → green overlay** on the field: **red = low health/greenness (needs
  attention)**, **green = healthy**. Turns one number into a map so you can *see* the good and bad patches.
- **Grid zones** — the field split into small squares, each coloured by greenness. **Click a square**
  to read that exact spot's value and a plain note (Low / Moderate / Healthy). Pinpoints *where* the
  problem is so you treat only those spots.

> Say to a farmer: *"The number tells you the field's average. This map tells you **where** the good
> and struggling areas are — so you spend time and inputs only where they're needed."*

---

## Season Progress (the trend graphs)

Each index above is plotted **over the season**, per field and for the whole farm, using **the date
each orthophoto was captured (the flight date)** — not the day it was uploaded. So you can fly/collect
all season and process later; the timeline still lines up with reality.

Each card shows a **trend badge**:
- **▲ Improving** (green) — the metric is moving the good way (up for health/greenness/canopy; down for weeds).
- **▼ Needs attention** (red) — moving the wrong way.
- **→ Holding steady** — little change.

> Say to a farmer: *"Green arrow = things are getting better; red arrow = worth a look. This is how you
> watch your whole farm's progress from week to week and spot trouble early."*

---

## What gets sent to the AgriTrack mobile app
For the farmer's phone we also translate the raw index into a simple **health score (0–1)** and a
**good / fair / poor** label, plus the VARI/canopy/weed numbers and the map images — so a farmer sees a
friendly status without needing any of the technical detail above.
