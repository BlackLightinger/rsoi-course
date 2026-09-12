#!/usr/bin/env bash
set -euo pipefail

PORT="${DASHBOARD_PROXY_PORT:-8001}"
URL="http://localhost:${PORT}/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/"

if ! kubectl get namespace kubernetes-dashboard >/dev/null 2>&1; then
  echo "Kubernetes Dashboard is not installed yet."
  echo "Run: make dashboard-install"
  exit 1
fi

if [ "${1:-}" = "--token-only" ]; then
  kubectl -n kubernetes-dashboard create token admin-user
  exit 0
fi

echo "Kubernetes Dashboard login token:"
echo
kubectl -n kubernetes-dashboard create token admin-user
echo
echo "Starting kubectl proxy on http://localhost:${PORT}"
echo "Dashboard URL:"
echo "  ${URL}"
echo
echo "Keep this terminal open while using Dashboard."

if command -v open >/dev/null 2>&1; then
  (sleep 2; open "$URL") >/dev/null 2>&1 &
fi

kubectl proxy --address=127.0.0.1 --port="$PORT"
