#!/usr/bin/env python3
"""Entry point compatible with the existing awg_bot systemd service."""
from manager.app import main

if __name__ == "__main__":
    raise SystemExit(main())
