"""Container entrypoint: serves the control API and owns every tunnel process."""

from __future__ import annotations

import logging
import os

import uvicorn

from .api import app, settings


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    uvicorn.run(app, host="0.0.0.0", port=settings.api_port, access_log=False)


if __name__ == "__main__":
    main()
