# Dashboard

The **Overview** tab shows what the station is reporting right now. It updates
itself every 30 seconds while the page is open and visible.

## Live status

The line at the top says how fresh the reading is. **Live** with a green dot
means the latest reading is less than 10 minutes old. If the station stops
reporting, it turns amber and says when the last reading arrived, and the tiles
show that last reading, slightly dimmed.

## The tiles

- **Temperature & Humidity**: the current temperature, *feels like* (labelled
  **wind chill** at 50 °F or colder with wind of 3 mph or more, **heat index**
  at 80 °F or warmer, otherwise the temperature itself; US National Weather
  Service formulas), today's high and low, humidity and dew
  point. The chart shows the last 24 hours of temperature, dew point and
  humidity; humidity uses its own 0–100% scale, so hover for exact values.
  When the dew point line meets the temperature line, the air is saturated
  (fog or dew).
- **Wind**: speed and the direction it's blowing *from*, the current gust and
  today's peak gust.
- **Rain**: today, the current storm, this month and this year, and the
  last 14 days as columns. Today, month and year totals match the console's
  own counters.
- **Pressure**: relative (sea-level) pressure, as the console reports it, and its trend over the last three hours
  (steady, rising, falling, or fast).
- **Sun**: UV index with its WHO category, solar radiation, and today's peak UV.
- **Indoors** (owner only): the console's indoor temperature and humidity.
- **More sensors**, when the station has any: extra temperature channels,
  soil, air quality, lightning and so on. See [Extra sensors](extra-sensors.md).

"Today" is the station's local day, midnight to midnight in its own time zone,
whatever time zone you're viewing from.

## A "Corrected" badge

If the owner has set up a temperature calibration for a faulty sensor (see
[Data quality and calibration](data-quality.md)), the live temperature is
corrected and marked **Corrected**.

## Missing values

A dash (—) means the station isn't reporting that measurement: the sensor
isn't fitted, its battery is flat, or the owner has excluded its readings as
faulty.
