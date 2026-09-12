#!/usr/bin/env bash
set -euo pipefail

DASHBOARD_VERSION="${DASHBOARD_VERSION:-v2.7.0}"
DASHBOARD_URL="https://raw.githubusercontent.com/kubernetes/dashboard/${DASHBOARD_VERSION}/aio/deploy/recommended.yaml"

cd "$(dirname "$0")/.."

echo "Installing Kubernetes Dashboard ${DASHBOARD_VERSION}..."
kubectl apply -f "$DASHBOARD_URL"
kubectl apply -f k8s/dashboard/admin-user.yaml

echo
echo "Waiting for Dashboard pods..."
kubectl -n kubernetes-dashboard rollout status deployment/kubernetes-dashboard --timeout=180s

echo
echo "Dashboard installed."
echo "Open it with:"
echo "  make dashboard-open"
echo
echo "Or print only the login token with:"
echo "  ./scripts/open-kubernetes-dashboard.sh --token-only"
