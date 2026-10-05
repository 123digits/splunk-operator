#!/usr/bin/env bash
# Local TLS lab driver. See README.md.
#   ./lab.sh up      generate PKI + defaults, start the 5 containers
#   ./lab.sh wait    block until every container's ansible run has finished
#   ./lab.sh check   functional checks + TLS probes (all REST calls verify)
#   ./lab.sh down    remove containers and network
# Needs: docker with compose v2, bash. ~10 GB RAM free. First start ~10 min.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=splunk/splunk:10.4.4
NODES="lab-lm lab-cm lab-idx0 lab-idx1 lab-sh"
D=.splunk.svc.cluster.local
PW='Tls-Lab-Passw0rd!'
# Git Bash on Windows rewrites /paths in docker args; harmless elsewhere.
export MSYS_NO_PATHCONV=1

host() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }
plain() { sed 's/\x1b\[[0-9;]*m//g'; }

up() {
  rm -rf .generated && mkdir -p .generated/pki .generated/defaults
  # PKI, generated inside the splunk image (python3 + cryptography).
  docker run --rm -v "$(host "$PWD/.generated/pki"):/pki" -v "$(host "$PWD/gen-pki.py"):/gen-pki.py:ro" \
    --entrypoint python3 "$IMAGE" /gen-pki.py
  # The ConfigMap data from ../02-tls-defaults.yaml, one file per key - the
  # same files the CRs mount at /mnt/tls-defaults. Never a hand-made copy.
  docker run --rm -v "$(host "$PWD/.."):/tls:ro" -v "$(host "$PWD/.generated/defaults"):/out" \
    --entrypoint python3 "$IMAGE" -c '
import yaml
for d in yaml.safe_load_all(open("/tls/02-tls-defaults.yaml")):
    if d and d.get("kind") == "ConfigMap":
        for k, v in d["data"].items():
            open("/out/" + k, "w").write(v); print("defaults:", k)'
  docker compose up -d
}

wait_done() {
  local n
  while :; do
    n=0
    for c in $NODES; do
      docker logs "$c" 2>&1 | grep -q "PLAY RECAP" && n=$((n+1))
      [ "$(docker inspect -f '{{.State.Running}}' "$c")" = true ] || { echo "$c exited"; break 2; }
    done
    [ "$n" -eq 5 ] && break
    sleep 15
  done
  for c in $NODES; do
    echo "$c: $(docker logs "$c" 2>&1 | plain | grep -A1 'PLAY RECAP' | tail -1 | tr -s ' ')"
  done
}

rest() { docker exec lab-sh curl -s --cacert /mnt/splunk-ca/ca.crt -u "admin:$PW" "$@"; }

check() {
  local CMH=splunk-cm-cluster-manager-service$D SHH=splunk-shc-search-head-service$D
  echo "### 1. ansible: result per node, and the restart check that used to fail"
  for c in $NODES; do
    echo "  $c: $(docker logs "$c" 2>&1 | plain | grep -A1 'PLAY RECAP' | tail -1 | tr -s ' ')"
    echo "      check_for_required_restarts: $(docker logs "$c" 2>&1 | plain | grep -A1 'TASK \[Check for required restarts\]' | grep -oE '^(ok|changed|fatal|FAILED)[^:]*' | sort | uniq -c | tr -s ' ' | paste -sd, -)"
  done
  echo "### 2. effective config (btool)"
  for c in lab-cm lab-idx0; do
    echo "  -- $c"
    docker exec -u splunk "$c" /opt/splunk/bin/splunk btool server list sslConfig 2>/dev/null \
      | grep -E "^(sslVersions|sslVersionsForClient|cipherSuite|sslVerifyServerCert|sslVerifyServerName|requireClientCert|serverCert|sslRootCAPath) " | sed 's/^/     /'
    docker exec -u splunk "$c" /opt/splunk/bin/splunk btool server list tls1.3 2>/dev/null | grep cipherSuite | sed 's/^/     [tls1.3] /'
    docker exec -u splunk "$c" /opt/splunk/bin/splunk btool server list general 2>/dev/null | grep "^serverName" | sed 's/^/     /'
  done
  docker exec -u splunk lab-idx0 /opt/splunk/bin/splunk btool server list clustering 2>/dev/null | grep -E "^register_" | sed 's/^/     idx0 /'
  docker exec -u splunk lab-idx0 /opt/splunk/bin/splunk btool server list 2>/dev/null | grep -E "^\[replication_port" | sed 's/^/     idx0 /'
  echo "### 3. indexer cluster (CM)"
  rest "https://$CMH:8089/services/cluster/manager/health?output_mode=json" | docker exec -i lab-sh python3 -c '
import sys, json
c = json.load(sys.stdin)["entry"][0]["content"]
print("  ", {k: c[k] for k in ("all_peers_are_up", "replication_factor_met", "search_factor_met", "all_data_is_searchable") if k in c})'
  rest "https://$CMH:8089/services/cluster/manager/peers?output_mode=json" | docker exec -i lab-sh python3 -c '
import sys, json
for e in json.load(sys.stdin)["entry"]:
    c = e["content"]; print("   peer", c["label"], c["status"], c.get("host_port_pair"), "repl_ssl=%s" % c.get("is_replication_ssl", c.get("replication_use_ssl")))'
  echo "### 4. distributed search from the SH; events from every tier reach the indexers"
  rest "https://$SHH:8089/services/search/jobs/export" -d output_mode=csv \
    -d search='search index=_internal earliest=-30m | stats count by splunk_server host' | sed 's/^/   /'
  echo "### 5. KV store on the SH (4-cipher TLS 1.2 list)"
  rest "https://$SHH:8089/services/kvstore/status?output_mode=json" | docker exec -i lab-sh python3 -c '
import sys, json
c = json.load(sys.stdin)["entry"][0]["content"]["current"]; print("  ", c.get("status"))'
  echo "### 6. TLS / certificate errors in splunkd.log (ERROR/WARN)"
  for c in $NODES; do
    echo "  -- $c"
    docker exec "$c" bash -c "sudo grep -E ' (ERROR|WARN) ' /opt/splunk/var/log/splunk/splunkd.log | grep -iE 'X509|certificate|ssl|tls|handshake|host ?name' | sed -E 's/^[0-9-]+ [0-9:.]+ [+-][0-9]+ //; s/\[[0-9]+ [^]]*\]//' | cut -c1-180 | sort | uniq -c | sort -rn | head -5" | sed 's/^/     /'
  done
  echo "### 7. TLS probes against indexer 0 (versions, ciphers, client certs, pinning)"
  docker exec lab-sh python3 /opt/lab/tlsprobe.py
}

case "${1:-}" in
  up) up ;;
  wait) wait_done ;;
  check) check ;;
  down) docker compose down -v ;;
  *) echo "usage: $0 up|wait|check|down"; exit 2 ;;
esac
