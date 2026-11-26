"""Compile the configured site extension without altering the original map's identity."""
import json
import os
from pathlib import Path


def site_config(base):
    config = json.loads(Path(base).read_bytes())
    extension = os.environ.get("EDGE_CAMPUS_SITE_EXTENSION")
    if extension:
        site = json.loads(Path(extension).read_bytes())
        config.update(navigation_map_id=site["navigation_map_id"], area=site["area"], site=site,
                      observed_prefix=True)
    return config
