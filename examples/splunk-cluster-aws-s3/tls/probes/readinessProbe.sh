#!/bin/bash
# Verifying readiness probe.
#
# Differs from the shipped tools/k8_probes/readinessProbe.sh in one respect:
# it does NOT pass curl --insecure. It validates the splunkd certificate
# against our internal CA and requires the hostname to match, so a probe
# success is evidence we reached the real splunkd and not something else bound
# to the port.
#
# Requires the pod to mount the tier's cert secret at /mnt/splunk-tls and that
# cert to carry a localhost SAN (tls/01-certificates.yaml sets both).

CA_PATH="${SPLUNK_TLS_CA_PATH:-/mnt/splunk-tls/ca.crt}"

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
        # Fail closed: if the CA bundle is absent we do NOT silently fall back
        # to an unverified request.
        if [[ ! -r "$CA_PATH" ]]; then
            echo "readiness: CA bundle $CA_PATH unreadable; refusing unverified probe"
            exit 1
        fi
        curl --max-time 30 --fail --cacert "$CA_PATH" "https://localhost:8089/"
        exit $?
    fi
    curl --max-time 30 --fail "http://localhost:8089/"
    exit $?
;;
*)
    exit 1
esac
