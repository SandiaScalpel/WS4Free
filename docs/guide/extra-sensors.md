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

## Sensors on a second device (sensor gateways)

Your console doesn't have to be the only source. An Ecowitt gateway (GW1100,
GW2000…) can carry sensors your console doesn't support, such as a WH51 soil
moisture probe beside an Ambient Weather console. Add it to the station as a
**sensor gateway**:

1. **Manage → Console & uploads → Sensor gateways → Add a gateway.** Give it a
   name, such as "Soil gateway".
2. Enter the settings shown there in the gateway's web page or WS View Plus,
   under **Weather Services → Customized**: Ecowitt protocol, your server, the
   gateway's own path and port 80. The gateway only needs internet access, not
   access to your home network.

Only the gateway's **extra sensors** (and their batteries) are kept. Everything
else it sends is ignored: its own indoor temperature, humidity and pressure, and
any outdoor sensor array it happens to hear (a gateway adopts the first sensors
of each kind it receives, which may be your console's array or a neighbour's).
So it never changes your station's main readings or its rain. Its sensors then
appear like your console's own: on the dashboard, in charts, the almanac and
reports, named and public or private under **Settings → Extra sensors**.

Each gateway has its own upload path and learns its own PASSKEY, like a console.
**New path** replaces a path that has leaked; **Remove** stops accepting its
uploads and keeps the readings it already sent.

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

The owner, managers and advanced viewers also see **Low battery** warnings for
any sensor whose console reports it.

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

Sensors that report their battery in volts (Ecowitt soil moisture, probe and
leaf wetness sensors, which run on one AA cell) also show the voltage under
their reading on the owner's dashboard: green while it's fine, red with
"replace" below 1.2 V. Watching it fall over the months tells you when a
battery change is coming.
