"""
Entry point for the xsphere slow control service.

Usage:
    python -m slowcontrol.app                    # default config
    python -m slowcontrol.app -c config.yaml     # custom config path
    python -m slowcontrol.app -v                 # verbose (DEBUG) logging
"""

import argparse
import logging
import sys

from slowcontrol.core import config as cfg_mod
from slowcontrol.core.service import SlowControlService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="xsphere slow control service"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    config = cfg_mod.load(args.config)

    log_level = logging.DEBUG if args.verbose else getattr(
        logging, config.log_level.upper(), logging.INFO
    )
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    service = SlowControlService(config)
    service.run()


if __name__ == "__main__":
    main()
