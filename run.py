#!/usr/bin/env python3
"""Entry point: start the Arena Sports Bot server."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.server import run

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    run(host="0.0.0.0", port=port)
