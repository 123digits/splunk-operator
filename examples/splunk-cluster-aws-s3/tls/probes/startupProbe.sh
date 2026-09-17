#!/bin/bash
# Verifying startup probe - see readinessProbe.sh for the rationale.
# Validates the splunkd cert against the internal CA instead of --insecure.
#
# Note the ordering problem this one has and the others do not: the startup
# probe runs before splunkd has necessarily loaded its configuration. If the
# cert is not yet in place, SCHEME resolves to http and no verification is
# attempted - correct, because there is nothing yet to authenticate. Once
# splunkd is up with SSL, every probe verifies.

CA_PATH="${SPLUNK_TLS_CA_PATH:-/mnt/splunk-tls/ca.crt}"

if [[ -n "$NO_HEALTHCHECK" ]]; then
    exit 0
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
        if [[ ! -r "$CA_PATH" ]]; then
            echo "startup: CA bundle $CA_PATH unreadable; refusing unverified probe"
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
