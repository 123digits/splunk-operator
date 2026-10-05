#!/bin/bash
# Verifying readiness probe.
#
# Differs from the shipped tools/k8_probes/readinessProbe.sh in one respect:
# it does NOT pass curl --insecure. It validates the splunkd certificate
# against the org CA bundle and requires the hostname to match, so a probe
# success is evidence we reached the real splunkd and not something else bound
# to the port.
#
# The SAN policy allows only *.svc.cluster.local names - no localhost, no
# 127.0.0.1 - so the probe names the pod by its own FQDN
# (<pod>.<headless-svc>.<ns>.svc.cluster.local, which 01-certificates.yaml
# puts in every tier cert) and uses --resolve to pin that name to 127.0.0.1.
# The connection never leaves the pod; only the name being verified changes.
#
# The FQDN comes from /etc/hosts, where the kubelet writes it for StatefulSet
# pods. Override with SPLUNK_TLS_PROBE_HOST if your cluster differs. If no FQDN
# can be found the probe fails rather than falling back to an unverified call.

CA_PATH="${SPLUNK_TLS_CA_PATH:-/mnt/splunk-ca/ca.crt}"

probe_host() {
    if [[ -n "$SPLUNK_TLS_PROBE_HOST" ]]; then
        echo "$SPLUNK_TLS_PROBE_HOST"
        return
    fi
    # Read /etc/hosts directly rather than `getent hosts`: getent asks for IPv6
    # first and nss-myhostname answers ::1 for the short name, hiding the
    # kubelet-written "<ip> <pod>.<headless>.<ns>.svc.<domain> <pod>" line.
    awk -v h="$HOSTNAME" '!/^[[:space:]]*#/ {
        for (i = 2; i <= NF; i++)
            if (index($i, h ".") == 1 && $i ~ /\.svc\./) { print $i; exit }
    }' /etc/hosts
}

if [[ -n "$NO_HEALTHCHECK" ]]; then
    exit 0
fi

[[ -f $SPLUNK_OPERATOR_K8_LIVENESS_DRIVER_FILE_PATH ]] && source $SPLUNK_OPERATOR_K8_LIVENESS_DRIVER_FILE_PATH
if [[ "1" == "$K8_OPERATOR_LIVENESS_LEVEL" ]]; then
    /bin/grep started /opt/container_artifact/splunk-container.state
    exit $?
fi

if [[ "false" == "$SPLUNKD_SSL_ENABLE" || \
      "false" == "$(/opt/splunk/bin/splunk btool server list | grep enableSplunkdSSL | cut -d\  -f 3)" ]]; then
    SCHEME="http"
else
    SCHEME="https"
fi

state="$(< $CONTAINER_ARTIFACT_DIR/splunk-container.state)"
case "$state" in
running|started)
    if [[ "$SCHEME" == "https" ]]; then
        # Fail closed: if the CA bundle or our own name is missing we do NOT
        # silently fall back to an unverified request.
        if [[ ! -r "$CA_PATH" ]]; then
            echo "readiness: CA bundle $CA_PATH unreadable; refusing unverified probe"
            exit 1
        fi
        HOST="$(probe_host)"
        if [[ -z "$HOST" ]]; then
            echo "readiness: no .svc FQDN for $HOSTNAME; refusing unverified probe"
            exit 1
        fi
        curl --max-time 30 --fail --cacert "$CA_PATH" \
            --resolve "$HOST:8089:127.0.0.1" "https://$HOST:8089/"
        exit $?
    fi
    curl --max-time 30 --fail "http://localhost:8089/"
    exit $?
;;
*)
    exit 1
esac
