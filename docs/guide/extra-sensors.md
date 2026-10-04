# Extra sensors

Many stations have more than the main outdoor sensors: extra temperature and
humidity channels, soil probes, leaf wetness, air quality monitors, a
lightning detector or leak sensors. WS4Free records whatever the console
sends and shows the ones it recognises.

## Supported sensors

| Kind | Ambient Weather | Ecowitt | WeeWX |
|---|---|---|---|
| Temperature and humidity channels | channels 1–10 | WH31 (channels 1–8), WH45 | |
| Soil temperature | ✓ | WN34 probes | first four sensors |
| Soil moisture | ✓ | WH51 | |
| Soil tension (centibars) | ✓ | | first four soil moisture sensors |
| Leaf wetness | ✓ | WN35 | first two sensors |
| PM2.5 / PM10 | outdoor, indoor and AQIN | WH41, WH43, WH45 | PM2.5, PM10 |
| CO₂ | ✓, AQIN | WH45, console | |
| Lightning | ✓ | WH57 | |
| Leak detectors | ✓ | WH55 | |

Sensors appear by themselves as soon as the console reports them. The first
time a new kind of sensor appears, WS4Free summarises the whole history for it
in the background, so charts of earlier periods fill in a few minutes later.

## Naming them and choosing who sees them

Under **Manage → Settings → Extra sensors**, each sensor has a name (for
example "Greenhouse" instead of "Temperature 2") and a **Public** switch.

New temperature and humidity channels, indoor air quality and CO₂ start
**private**, because they're often inside the house; soil, leaf wetness,
outdoor air quality and lightning start public. Private sensors are shown only
to you, on the dashboard, in charts and in CSV downloads.

## On the dashboard

The **More sensors** section shows each sensor's current reading, grouped by
kind:

- temperature, humidity and soil channels with today's range;
- PM2.5 with its US EPA air-quality category (*Good*, *Moderate*, …), based on
  the 24-hour average when the sensor reports one;
- lightning strikes so far today, and the time and distance of the last strike;
- leak sensors as *Dry*, *Leak!* or *Offline*.

The owner also sees **Low battery** warnings for any sensor whose console
reports it.

## In charts

Each kind of sensor gets its own chart in History, with every channel as its
own line, and its own columns in the CSV download. Lightning is shown as
strikes per 5 minutes, hour or day.

## In the almanac and reports

- **Almanac → Records** has an **Extra sensors** section: the highest and lowest
  reading of each extra temperature probe (with the time), the highest PM2.5,
  PM10 and CO₂, and the most lightning strikes in a day. All time, or for one year.
- **Reports → Columns** lists the station's extra sensors too: high, low and
  mean for temperature probes, the mean of humidity, soil and leaf wetness, mean
  and peak particulates, mean CO₂, and lightning strikes. They're in the CSV
  download as well.

Private sensors appear in both only for you.

## When a sensor goes wrong

Extra sensors can be excluded like the main ones: on **Data quality**, each
sensor is listed with the other measurements. Its readings are set aside for
the period (and from new uploads, for an ongoing problem), and come back
exactly if you remove the exclusion. A record that looks wrong has a **Not
right? Exclude these readings…** link that fills this in.

## Batteries

The owner sees a **Low battery** warning on the dashboard, and on the Charts
tab the periods when any sensor reported a low battery are shaded, with the
sensor named in the tooltip, so gaps or odd readings at those times are easy to
explain. Visitors don't see either.
