#!/bin/bash
# Kept for the commands people already type: scripts/ice_workers.sh <mode> ... is
# scripts/pace_workers.sh ice <mode> ... (which also queues Phoenix nodes).
exec "$(dirname "${BASH_SOURCE[0]}")/pace_workers.sh" ice "$@"
