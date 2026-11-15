"""The ingest log's shape. Against a real PostGIS (see test_sync_integration.py)."""

import psycopg2
import pytest

from geodata_sync import ingest
from test_sync_integration import DB, query

pytestmark = pytest.mark.integration


def test_the_log_is_indexed_by_dataset():
    conn = psycopg2.connect(**DB)
    try:
        ingest.bootstrap(conn)
        ingest.bootstrap(conn)  # and bootstrapping again is harmless
    finally:
        conn.close()
    assert query("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'os_ingest_log_dataset_started'") \
        == [("CREATE INDEX os_ingest_log_dataset_started ON public.os_ingest_log USING btree (dataset_id, started_at)",)]
