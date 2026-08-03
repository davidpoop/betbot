#!/bin/bash
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/.venv/bin/python" -m betbot.launcher --stop
