from __future__ import annotations

from prometheus_client import Counter, Gauge

NODES_CONNECTED = Gauge("millwright_nodes_connected", "Transcode nodes currently connected to the hub.")
JOBS_FINISHED = Counter("millwright_jobs_finished", "Terminal job results reported by nodes.", ["result"])
EVENTS_SENT = Counter("millwright_events_sent", "Events published to the /api/events bus.", ["type"])
