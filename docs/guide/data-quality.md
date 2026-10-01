# Data quality and calibration

Sensors fail. A humidity sensor sticks at one value, a rain gauge clogs, a
replacement temperature sensor reads too warm in the sun. The **Data quality**
tab (under **Manage**) has two tools: **exclusions** for readings that are
simply wrong, and **temperature calibration** for a sensor that reads wrong in
a consistent way.

Both are reversible: nothing is ever deleted.

## Excluding readings

An exclusion sets aside a period's readings for chosen measurements:

- outdoor temperature;
- outdoor humidity;
- wind;
- rain;
- pressure;
- sun and UV;
- indoor.

Choose the measurements, the start, and either an end or leave the end blank
for a problem that's still going on (new readings are then set aside as they
arrive). Add a reason so you remember why.

Excluded readings are treated as missing everywhere: charts show a gap, they
can't set records or frost dates, reports count those days as incomplete, and
the dashboard shows a dash. **Remove** puts every reading back exactly as it
was. Applying or removing a long exclusion runs in the background; charts and
records catch up within about five minutes.

Dew point depends on both temperature and humidity, so excluding either one
also sets aside the dew point.

> Readings older than the server's raw-data retention period (if your
> administrator has set one) are merged into 15-minute records, and an
> exclusion over them can't restore the original 5-minute values.

## Temperature calibration

Some sensors aren't broken, just biased. A sensor in a poor radiation shield,
for example, reads several degrees too warm in strong sunshine and slightly too
cold on clear nights. Excluding months of its readings would throw away good
information; calibration corrects them instead.

The correction depends on the time of day and the sunshine, month by month:

> correction = night offset + daytime offset (when the sun is up) + solar slope
> × solar radiation

It has three numbers for each month: the **night offset**, the extra
**daytime offset**, and the **solar slope** (how much more the error grows per
1000 W/m² of sunshine). At night only the night offset applies. Where the
station has no solar reading, whether the sun is up is worked out from its
latitude and longitude.

### Fitted to a reference station (recommended)

WS4Free can work out the correction by comparing your station hour by hour
with a nearby official weather station: an airport station from the
[Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) archive. Only
the station identifier and dates are sent to it.

1. **Affected from / until**: when your sensor started reading wrong, for
   example when it was installed. Leave *until* blank if it still does.
2. **Reference station**: the identifier of a nearby airport, for example `DEN`
   for Denver (US stations are entered without the leading K; `KDEN` works too).
   Choose one with similar terrain and elevation if you can.
3. **Trusted from / until**: a period when your sensor read correctly, ideally
   a full year. This teaches WS4Free the natural difference between your site
   and the airport (your garden may simply be cooler at night), so only the
   sensor's error is corrected, not your microclimate.

**Fit correction** fetches the reference data and fits the numbers. This takes
a few minutes. Nothing changes until you've reviewed the result:

- **Typical error before / after**: how far your hourly readings are from the
  reference, before and after the correction. It's measured on weeks the fit
  didn't use, so it's an honest test.
- **Best achievable**: the same comparison during your trusted period. Two
  sites never match exactly, so this is the floor.
- **Correction by month**: the three numbers for each month, and the total at
  a sunny noon.

If the correction barely helps, you'll see a warning. Otherwise choose
**Apply**, or **Discard** to throw it away.

### Manual

If there's no reference station nearby, choose **Manual** and enter the three
numbers yourself, in your temperature unit; they apply to every month. For
example, a sensor that reads 2 °F cold at night and 6 °F warm at a sunny noon
might use a night offset of −2, a daytime offset of 4 and a solar slope of 4.

### What changes

Once applied, the corrected temperatures (and the dew points calculated from
them) are used everywhere: dashboard, charts, records, reports, growing degree
days. New readings are corrected as they arrive if the period has no end.
Charts, the almanac, reports and the growing page note which periods are
corrected, and the live temperature shows a **Corrected** badge.

**Remove** restores every original reading exactly. Calibrations and
exclusions work together: an excluded reading stays excluded, and if you later
remove the exclusion, the restored readings are corrected too.

A correction can only be as good as its model: it removes the predictable part
of the error, and individual hours can still be off. If a sensor's shield is
the problem, fixing the hardware is the real cure; then set the calibration's
end date to the day you fixed it.
