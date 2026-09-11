"""
Configuration for instrument-master-loader.

Only this function's own tunables. The Neon connection and the shared
connection-string read live in the neon-access layer - see neon_access.
"""

import os


SCRIP_MASTER_URL = os.environ.get(
    "DHAN_SCRIP_MASTER_URL",
    "https://images.dhan.co/api-data/api-scrip-master.csv",
)

# Rows are pushed in batches of this many to keep a single INSERT statement
# (and its parameter list) within sane limits. The filtered master is ~10^5
# rows, dominated by OPTIDX.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "5500"))

DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "120"))
