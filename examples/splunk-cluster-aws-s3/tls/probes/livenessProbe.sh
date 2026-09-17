#!/bin/bash
# Verifying liveness probe - see readinessProbe.sh for the rationale.
# Validates the splunkd cert against the internal CA instead of --insecure.

CA_PATH="${SPLUNK_TLS_CA_PATH:-/mnt/splunk-tls/ca.crt}"

[[ -f $SPLUNK_OPERATOR_K8_LIVENESS_DRIVER_FILE_PATH ]] && source $SPLUNK_OPERATOR_K8_LIVENESS_DRIVER_FILE_PATH

get_http_proto_type() {
    if [[ "false" == "$SPLUNKD_SSL_ENABLE" || \
          "false" == "$(/opt/splunk/bin/splunk btool server list | grep enableSplunkdSSL | cut -d\  -f 3)" ]]; then
        echo "http"
    else
        echo "https"
    fi
}

liveness_probe_check_splunkd_process() {
    SPLUNK_PROCESS_ID=$(ps ax | grep "splunkd.*start" | grep -v grep | head -1 | awk '{print $1}')
    state="$(< $CONTAINER_ARTIFACT_DIR/splunk-container.state)"
    case "$state" in
    running|started)
        if [[ -n "$SPLUNK_PROCESS_ID" ]]; then
            exit 0
        fi
        echo "Splunkd not running"
        exit 1
    ;;
    *)
        exit 1
    esac
}

liveness_probe_default() {
    HTTP_SCHEME=$(get_http_proto_type)
    state="$(< $CONTAINER_ARTIFACT_DIR/splunk-container.state)"

    case "$state" in
    running|started)
        if [[ "$HTTP_SCHEME" == "https" ]]; then
            if [[ ! -r "$CA_PATH" ]]; then
                echo "liveness: CA bundle $CA_PATH unreadable; refusing unverified probe"
                exit 1
            fi
            curl --max-time 30 --fail --cacert "$CA_PATH" "https://localhost:8089/"
        else
            curl --max-time 30 --fail "http://localhost:8089/"
        fi
        if [[ $? == 0 ]]; then
            exit 0
        fi
        echo "Mgmt. port is not reachable or certificate failed verification"
        exit 1
    ;;
    *)
        exit 1
    esac
}

if [[ -n "$NO_HEALTHCHECK" ]]; then
    exit 0
fi

case $K8_OPERATOR_LIVENESS_LEVEL in
1) liveness_probe_check_splunkd_process ;;
*) liveness_probe_default ;;
esac
