#!/bin/bash
cd "$(dirname "$0")"
PORT=${1:-8080}
echo "╔═══════════════════════════════════════╗"
echo "║  LLM Load Tester                      ║"
echo "║  http://localhost:${PORT}              ║"
echo "╚═══════════════════════════════════════╝"
python3 -m http.server "$PORT"
