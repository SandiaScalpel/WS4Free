# Growing

The **Growing** tab is for gardeners, farmers and orchardists.

## Growing degree days

Growing degree days (GDD) measure the warmth crops and insects need to develop.
Choose a crop:

| Preset | Base | Cap |
|---|---|---|
| Corn / general | 50 °F | 86 °F |
| Wheat, barley, oats | 32 °F | none |
| Alfalfa | 41 °F | none |
| Cotton | 60 °F | 86 °F |
| Wine grapes (Winkler index) | 50 °F | none, April–October only |

The page shows this season's total, the average to the same date in earlier
seasons, and a chart comparing every season. Days use the *modified* method:
the high is capped and the low is raised to the base before averaging.

## Winter chill

Fruit trees need enough winter cold to flower well. Choose **chill hours**
(hours between 32 °F and 45 °F) or **Utah chill units** (which also subtract
warm hours). The season runs November–February (May–August in the southern
hemisphere).

## Evapotranspiration

**Reference evapotranspiration (ET₀)** estimates how much water a well-watered
grass surface loses to the air each day, the starting point for irrigation
planning. WS4Free uses the FAO-56 Penman–Monteith method, from temperature,
humidity, wind, solar radiation, latitude and elevation. When a measurement is
missing it falls back to the temperature-only Hargreaves method; both are
shown.

Penman–Monteith assumes the wind sensor sees open-field wind at 2 metres. Set
the sensor's height in the station's settings. A sheltered backyard
anemometer reads low wind and makes ET₀ low; the Hargreaves figure isn't
affected by siting.

The page compares rain with ET₀ for the last 30 days and by month. Growing
degree days and ET₀ are also available as [report](reports.md) columns.
