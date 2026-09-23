#!/bin/sh
set -eu

container_name="nupson-integration"
test_port="18081"
cookie_file="$(mktemp)"
archive_file=""

cleanup() {
    docker rm -f "$container_name" >/dev/null 2>&1 || true
    rm -f "$cookie_file"
    if [ -n "$archive_file" ]; then
        rm -f "$archive_file"
    fi
}
trap cleanup EXIT INT TERM

docker run -d --name "$container_name" \
    -p "$test_port:8080" \
    -v "$(pwd)/tests/fixtures/outage.seq:/tmp/outage.seq:ro" \
    nupson:test >/dev/null

attempt=0
until curl -fsS "http://127.0.0.1:$test_port/healthz" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$container_name"
        exit 1
    fi
    sleep 1
done

curl -fsS -c "$cookie_file" -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"integration-password"}' \
    "http://127.0.0.1:$test_port/api/auth/setup" >/dev/null

configure_response="$(curl -fsS -b "$cookie_file" -H 'Content-Type: application/json' \
    -d '{"name":"ups","driver":"dummy-ups","port":"/tmp/outage.seq","desc":"Integration UPS","client_password":"integration-nut-password"}' \
    "http://127.0.0.1:$test_port/api/ups/configure")"

if printf '%s' "$configure_response" | grep -q 'integration-nut-password'; then
    echo "NUT password leaked in API response"
    exit 1
fi

attempt=0
until docker exec "$container_name" upsc ups@127.0.0.1 2>/dev/null | grep -q 'ups.status: OL'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$container_name"
        exit 1
    fi
    sleep 1
done

curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/status" | grep -q 'NUPSON Integration Test'
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/settings" | grep -q '"history_retention_days":180'
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/settings" | grep -q '"outage_reminder_minutes":15'
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/history" | grep -q '"summary"'
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/webhook/scenarios" \
    | grep -q 'host_unreachable'
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/" | grep -q 'Na żywo · ostatnia godzina'

profile_response="$(curl -fsS -b "$cookie_file" -H 'Content-Type: application/json' \
    -d '{"name":"Integration client","address":"192.0.2.44","server_address":"127.0.0.1","platform":"debian","policy":"timer","delay_seconds":60,"final_delay_seconds":5}' \
    "http://127.0.0.1:$test_port/api/client-profiles")"
profile_id="$(printf '%s' "$profile_response" | sed -n 's/.*"id":\([0-9][0-9]*\).*/\1/p')"
test -n "$profile_id"
curl -fsS -b "$cookie_file" \
    "http://127.0.0.1:$test_port/api/client-profiles/$profile_id/config" \
    | grep -q 'START-TIMER nupson-shutdown 60'
archive_file="$(mktemp)"
curl -fsS -b "$cookie_file" -o "$archive_file" \
    "http://127.0.0.1:$test_port/api/client-profiles/$profile_id/download"
test -s "$archive_file"
rm -f "$archive_file"
archive_file=""
curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/client-profiles" \
    | grep -q 'Integration client'

curl -fsS -b "$cookie_file" -H 'Content-Type: application/json' \
    -d '{"stabilization_seconds":2,"minimum_charge":0,"poll_seconds":1,"wake_attempts":1,"wake_interval_seconds":1,"wave_delay_seconds":0}' \
    "http://127.0.0.1:$test_port/api/settings" >/dev/null

curl -fsS -b "$cookie_file" -H 'Content-Type: application/json' \
    -d '{"name":"Integration host","mac":"AA:BB:CC:DD:EE:FF","broadcast":"127.255.255.255","policy":"always","wave":1,"delay_seconds":0}' \
    "http://127.0.0.1:$test_port/api/hosts" >/dev/null

attempt=0
until curl -fsS -b "$cookie_file" "http://127.0.0.1:$test_port/api/events?limit=100" \
    | grep -q 'Wake sequence completed'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 25 ]; then
        docker logs "$container_name"
        exit 1
    fi
    sleep 1
done

echo "NUPSON integration test passed"
