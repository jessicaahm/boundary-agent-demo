#!/usr/bin/env bash
# Make sure the minikube cluster is up, and start it if it is not.
#
#   mcp/target.sh                              <- run it; safe to run twice
#   MINIKUBE_PROFILE=demo mcp/target.sh
#   mcp/target.sh --cpus 4 --memory 8192       <- args go to `minikube start`
#
# Idempotent on purpose: this is a precondition you can put in front of
# anything that needs the cluster, not a one-shot setup step.
#
# `minikube status` is the whole test. It exits 0 only when the host, kubelet
# and apiserver are all running AND the kubeconfig points at them, so a single
# exit code already covers every way the cluster can be unusable. The nonzero
# codes are deliberately NOT told apart: a profile that was never created (85),
# a stopped VM and a half-up cluster all take the same remedy, and
# `minikube start` is itself idempotent -- against a healthy cluster it just
# reconciles and returns.
#
# Status is read as an exit code rather than parsed, and that is not laziness:
# with no profile created, `minikube status -o json` emits a *stream* of
# cloudevent objects instead of the status object, so the usual
# `minikube status -o json | jq -r .Host` fails exactly when the cluster is
# most broken. The exit code stays correct in that case.

set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-minikube}"

log() { printf '[mcp] %s\n' "$*"; }

if ! command -v minikube >/dev/null 2>&1; then
  log "minikube is not installed -- https://minikube.sigs.k8s.io/docs/start/"
  exit 1
fi

if minikube status -p "$PROFILE" >/dev/null 2>&1; then
  log "minikube profile '${PROFILE}' is already running"
  exit 0
fi

# Any argument given to this script is forwarded to `minikube start`. Note they
# only take effect when a start actually happens: an already-running cluster
# returns above untouched, which is the idempotence this script is for.
log "minikube profile '${PROFILE}' is not running -- starting it"
minikube start -p "$PROFILE" "$@"

# Started is not the same as serving, and `minikube start` can return 0 having
# left a component down, so re-assert the same check this script opened with
# instead of trusting that exit code.
if ! minikube status -p "$PROFILE" >/dev/null 2>&1; then
  log "started, but profile '${PROFILE}' is still not healthy:"
  minikube status -p "$PROFILE" || true
  exit 1
fi

log "minikube profile '${PROFILE}' is up"
minikube status -p "$PROFILE"

kubectl run nginx --image=nginx
# expose the kubernetes api-server to the local machine so that the dashboard can be accessed
kubectl proxy --disable-filter=true
# Make the kubernetes api publicly accessible so that the dashboard can be accessed
ngrok http --scheme=http 127.0.0.1:8001
# For K8S Secret Engine
kubectl create namespace vault
kubectl create -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: vault
  namespace: vault
---
apiVersion: v1
kind: Secret
metadata:
  name: vault
  namespace: vault
  annotations:
    kubernetes.io/service-account.name: vault
type: kubernetes.io/service-account-token
EOF
# Create cluster role binding for vault service account
kubectl create -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-full-secrets-abilities-with-labels
rules:
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["serviceaccounts", "serviceaccounts/token"]
  verbs: ["create", "update", "delete"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["rolebindings", "clusterrolebindings"]
  verbs: ["create", "update", "delete"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "clusterroles"]
  verbs: ["bind", "escalate", "create", "update", "delete"]
EOF
#Associate service account with cluster role binding
kubectl create -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: vault-token-creator-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: k8s-full-secrets-abilities-with-labels
subjects:
- kind: ServiceAccount
  name: vault
  namespace: vault
EOF
KUBE_VAULT_SECRET=$(kubectl get secret -n vault vault -o json | jq -r '.data')
KUBE_CA_CRT=$(echo $KUBE_VAULT_SECRET | jq -r '."ca.crt"' | base64 -d)
KUBE_VAULT_TOKEN=$(echo $KUBE_VAULT_SECRET | jq -r '.token' | base64 -d)

echo KUBE_API_URL=$KUBE_API_URL && echo KUBE_VAULT_TOKEN=$KUBE_VAULT_TOKEN && echo KUBE_CA_CRT=$KUBE_CA_CRT
vault secrets enable kubernetes
vault write -f kubernetes/config \
    kubernetes_host=$KUBE_API_URL \
    kubernetes_ca_cert=$KUBE_CA_CRT \
    service_account_jwt=$KUBE_VAULT_TOKEN
vault write kubernetes/roles/auto-managed-sa-and-role \
allowed_kubernetes_namespaces="*" \
token_default_ttl="10m" \
generated_role_rules='{"rules":[{"apiGroups":[""],"resources":["pods"],"verbs":["list"]}]}'
vault write kubernetes/creds/auto-managed-sa-and-role \
    kubernetes_namespace=default
