"""Payloads modelled on real traffic. The API record is a WS-2902's actual
lastData (MAC anonymised); push payloads follow the same console's fields."""
import datetime as dt
import hashlib
from urllib.parse import parse_qsl

MAC = '48:3F:DA:12:34:56'
ECOWITT_PASSKEY = hashlib.md5(MAC.encode()).hexdigest().upper()

API_RECORD = {
    'baromabsin': 24.986, 'baromrelin': 29.569, 'battout': 1, 'dailyrainin': 0.12,
    'date': '2026-10-01T04:40:00.000Z', 'dateutc': 1790829600000, 'dewPoint': 52.64,
    'dewPointin': 49.3, 'eventrainin': 2.56, 'feelsLike': 58.8, 'feelsLikein': 72.2,
    'hourlyrainin': 0, 'humidity': 80, 'humidityin': 43, 'lastRain': '2026-09-30T09:41:00.000Z',
    'maxdailygust': 18.3, 'monthlyrainin': 5.89, 'solarradiation': 0, 'tempf': 58.8,
    'tempinf': 73.2, 'totalrainin': 47.571, 'tz': 'America/Denver', 'uv': 0,
    'weeklyrainin': 2.57, 'winddir': 289, 'windgustmph': 4.5, 'windspeedmph': 3.1,
}

AMBIENT_QUERY = (
    f'PASSKEY={MAC}&stationtype=AMBWeatherPro_V5.0.6&dateutc=2026-09-30+12:03:17'
    '&tempf=58.8&humidity=80&windspeedmph=3.1&windgustmph=4.5&maxdailygust=18.3&winddir=289'
    '&uv=0&solarradiation=0.0&hourlyrainin=0.120&eventrainin=2.560&dailyrainin=0.120'
    '&weeklyrainin=2.570&monthlyrainin=5.890&totalrainin=47.571&battout=1&tempinf=73.2'
    '&humidityin=43&baromrelin=29.569&baromabsin=24.986'
)

ECOWITT_BODY = (
    f'PASSKEY={ECOWITT_PASSKEY}&stationtype=EasyWeatherPro_V5.1.6&runtime=3'
    '&dateutc=2026-09-30+12:03:17&tempinf=73.2&humidityin=43&baromrelin=29.569&baromabsin=24.986'
    '&tempf=58.8&humidity=80&winddir=289&windspeedmph=3.13&windgustmph=4.47&maxdailygust=18.34'
    '&solarradiation=0.00&uv=0&rainratein=0.120&eventrainin=2.560&hourlyrainin=0.050'
    '&dailyrainin=0.120&weeklyrainin=2.570&monthlyrainin=5.890&yearlyrainin=20.000'
    '&totalrainin=47.571&temp1f=65.3&humidity1=55&soilmoisture1=34&wh65batt=0&freq=915M'
    '&model=WS2900_V2.01.18&interval=60'
)

WU_QUERY = (
    'ID=KCOTEST99&PASSWORD=secret&action=updateraw&dateutc=now&tempf=58.8&humidity=80'
    '&dewptf=52.6&baromin=29.569&absbaromin=24.986&windspeedmph=3.1&windgustmph=4.5'
    '&winddir=289&rainin=0.05&dailyrainin=0.12&solarradiation=0&UV=0&indoortempf=73.2'
    '&indoorhumidity=43&softwaretype=WS-2902&realtime=1&rtfreq=5'
)

PUSH_NOW = dt.datetime(2026, 9, 30, 12, 3, 30, tzinfo=dt.UTC)


def as_dict(query):
    return dict(parse_qsl(query))
