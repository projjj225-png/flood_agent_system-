"""Offline fixtures shaped like the NWDP CKAN API responses.

PROVENANCE: the identifiers, field list, station attributes and the 30
"REAL_*" rows below were transcribed on 2026-10-13 from the portal's
``resource_show``/``package_show`` and ``datastore_search`` responses
(resource d027c5ac-379d-4ac2-8ced-97b02b6edbc0, filter Station=UPPER ANICUT,
sort _id desc, limit 30), as read through a web tool. They are a faithful
transcription for testing, not a byte-exact capture. Rows built by
``synthetic_records`` are FABRICATED test values and are labelled as such.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

DATASET_ID = "68600163-c5a0-4327-aa1a-fa7157b86cce"
RESOURCE_ID = "d027c5ac-379d-4ac2-8ced-97b02b6edbc0"
RESOURCE_NAME = "River Water Level CWC Cauvery (2026 - 2030) Telemetry Hourly"
LEVEL_FIELD = "River Water Level Telemetry Hourly (meter)"
TIME_FIELD = "Data Acquisition Time"
IST = timezone(timedelta(hours=5, minutes=30))

FIELD_IDS = ["_id", "SlNo", "Station", "Agency", "State LGD Code", "State",
             "District LGD Code", "District", "Tehsil", "Block", "Village", "River", "Basin",
             "Tributary", "Subtributary", "SubSubtributary", "Local River", "Latitude",
             "Longitude", "Is_DischargeDataAvailable", "RL_of_zeroGauge", "MeanSeaLevel",
             TIME_FIELD, LEVEL_FIELD]

STATION_ATTRS = {
    "Station": "UPPER ANICUT", "Agency": "CWC", "State LGD Code": "33", "State": "Tamil Nadu",
    "District LGD Code": "591", "District": "Tiruchirappalli", "Tehsil": "TIRUCHIRAPPALLI",
    "Block": "-", "Village": "-", "River": "Cauvery", "Basin": "Cauvery", "Tributary": "-",
    "Subtributary": "-", "SubSubtributary": "-", "Local River": "Cauvery",
    "Latitude": "10.88861111", "Longitude": "78.58000000", "Is_DischargeDataAvailable": "Yes",
    "RL_of_zeroGauge": "72.6000", "MeanSeaLevel": "0",
}

# (_id, Data Acquisition Time (IST, naive), level text) - transcribed real rows.
REAL_ROWS = [
    (21956, "03-06-2026 21:00", "324.879"), (21955, "03-06-2026 20:00", "71.721"),
    (21954, "03-06-2026 19:00", "350.878"), (21953, "03-06-2026 18:00", "71.741"),
    (21952, "03-06-2026 17:00", "330.876"), (21951, "03-06-2026 16:00", "326.873"),
    (21950, "03-06-2026 15:00", "316.872"), (21949, "03-06-2026 12:00", "322.871"),
    (21948, "03-06-2026 11:00", "318.873"), (21947, "03-06-2026 10:00", "320.874"),
    (21946, "03-06-2026 09:00", "330.876"), (21945, "03-06-2026 08:00", "71.748"),
    (21944, "03-06-2026 07:00", "323.879"), (21943, "03-06-2026 06:00", "330.879"),
    (21942, "03-06-2026 05:00", "71.841"), (21941, "03-06-2026 04:00", "71.919"),
    (21940, "03-06-2026 03:00", "152.879"), (21939, "03-06-2026 02:00", "71.92"),
    (21938, "03-06-2026 01:00", "151.879"), (21937, "03-06-2026 00:00", "71.918"),
    (21936, "02-06-2026 23:00", "71.918"), (21935, "02-06-2026 22:00", "153.879"),
    (21934, "02-06-2026 21:00", "71.919"), (21933, "02-06-2026 20:00", "71.918"),
    (21932, "02-06-2026 19:00", "71.92"), (21931, "02-06-2026 18:00", "151.878"),
    (21930, "02-06-2026 17:00", "71.912"), (21929, "02-06-2026 16:00", "159.874"),
    (21928, "02-06-2026 14:00", "71.865"), (21927, "02-06-2026 13:00", "206.871"),
]
REAL_LATEST_UTC = datetime(2026, 6, 3, 15, 30, tzinfo=timezone.utc)  # 03-06-2026 21:00 IST
REAL_TOTAL_FOR_STATION = 1696


def record(_id: int, time_text: str, level: str, **overrides) -> dict:
    return {"_id": _id, "SlNo": str(_id), **STATION_ATTRS, TIME_FIELD: time_text,
            LEVEL_FIELD: level, **overrides}


def real_records(**overrides) -> list[dict]:
    return [record(i, t, v, **overrides) for i, t, v in REAL_ROWS]


def synthetic_records(end_ist: datetime, levels: list[str], start_id: int = 50000) -> list[dict]:
    """FABRICATED hourly rows ending at end_ist (newest first, like sort=_id desc)."""
    n = len(levels)
    rows = []
    for k, level in enumerate(levels):
        moment = end_ist - timedelta(hours=n - 1 - k)
        rows.append(record(start_id + k, moment.strftime("%d-%m-%Y %H:%M"), level))
    return list(reversed(rows))


def resource_show_payload(**overrides) -> dict:
    result = {
        "id": RESOURCE_ID, "package_id": DATASET_ID, "name": RESOURCE_NAME, "format": "CSV",
        "datastore_active": True, "last_modified": "2026-10-13T00:20:54.787668",
        "created": "2026-02-02T16:31:32.264224",
        "url": (f"https://nwdp.nwic.gov.in/dataset/{DATASET_ID}/resource/{RESOURCE_ID}"
                "/download/rwl_tel_hr_cwc_009_2026_2030.csv"),
    }
    result.update(overrides)
    return {"help": "https://nwdp.nwic.gov.in/api/3/action/help_show?name=resource_show",
            "success": True, "result": result}


def datastore_payload(records: list[dict], fields: list[str] | None = None,
                      resource_id: str = RESOURCE_ID, total: int | None = None) -> dict:
    return {
        "help": "https://nwdp.nwic.gov.in/api/3/action/help_show?name=datastore_search",
        "success": True,
        "result": {
            "include_total": True, "limit": len(records), "records_format": "objects",
            "resource_id": resource_id,
            "fields": [{"id": f, "type": "int" if f == "_id" else "text"}
                       for f in (fields or FIELD_IDS)],
            "records": records,
            "_links": {"start": "/api/3/action/datastore_search"},
            "total": REAL_TOTAL_FOR_STATION if total is None else total,
            "total_was_estimated": False,
        },
    }


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, raw: bytes | None = None):
        self.status_code = status_code
        if raw is None:
            raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.content = raw
        self.text = raw.decode("utf-8", errors="replace")


class FakeSession:
    """Routes by CKAN action name. Each route is a list consumed in order;
    items are FakeResponse objects or exceptions to raise."""

    def __init__(self, routes: dict[str, list]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, dict, object]] = []

    def get(self, url, params=None, timeout=None):
        action = url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append((action, params, timeout))
        queue = self.routes.get(action)
        if not queue:
            raise AssertionError(f"unexpected call to {action}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok_session(records=None, resource=None) -> FakeSession:
    return FakeSession({
        "resource_show": [FakeResponse(200, resource or resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(
            real_records() if records is None else records))],
    })
