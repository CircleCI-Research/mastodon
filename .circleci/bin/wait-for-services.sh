#!/usr/bin/env bash
#
# Wait until each named service accepts connections.
#
# GitHub Actions service containers support `options: --health-cmd ...
# --health-retries ...`, and the runner will not start the job's steps until
# every service reports healthy. CircleCI secondary ("service") containers in
# the docker executor have no equivalent: the primary container's steps begin
# as soon as the containers are *started*, not when they are *ready*. This
# script is the replacement for those healthchecks.
#
# Usage:
#   wait-for-services.sh SPEC [SPEC ...]
#
# SPEC is one of:
#   tcp:HOST:PORT   - ready when a TCP connection is accepted
#   http:URL        - ready when curl gets a 2xx/3xx response
#
# Environment:
#   WAIT_TIMEOUT_SECONDS   per-service timeout, default 120
#   WAIT_INTERVAL_SECONDS  poll interval, default 1
#
# Deliberately avoids GNU-only tooling (no `timeout(1)`, no `getopt`, no GNU
# `date` arithmetic) so it behaves identically on the CircleCI Linux images and
# on a BSD/macOS host, where it is unit-tested.

set -eu

timeout_seconds="${WAIT_TIMEOUT_SECONDS:-120}"
interval_seconds="${WAIT_INTERVAL_SECONDS:-1}"

usage() {
  echo "usage: $(basename "$0") SPEC [SPEC ...]" >&2
  echo "  SPEC := tcp:HOST:PORT | http:URL" >&2
}

# Single readiness probe. Returns 0 when the service answered.
probe() {
  spec_kind="$1"
  spec_rest="$2"

  case "$spec_kind" in
    tcp)
      probe_host="${spec_rest%:*}"
      probe_port="${spec_rest##*:}"
      if [ -z "$probe_host" ] || [ -z "$probe_port" ]; then
        echo "wait-for-services: malformed tcp spec 'tcp:$spec_rest'" >&2
        return 2
      fi
      # bash's /dev/tcp pseudo-device; present in bash 3.2 and later.
      (exec 3<>"/dev/tcp/${probe_host}/${probe_port}") 2>/dev/null
      ;;
    http)
      curl --silent --show-error --fail --max-time 5 --output /dev/null "$spec_rest" 2>/dev/null
      ;;
    *)
      echo "wait-for-services: unknown spec kind '$spec_kind'" >&2
      return 2
      ;;
  esac
}

wait_for_one() {
  spec="$1"
  kind="${spec%%:*}"
  rest="${spec#*:}"

  if [ "$kind" = "$spec" ]; then
    echo "wait-for-services: malformed spec '$spec' (expected KIND:...)" >&2
    return 2
  fi

  started_at="$(date +%s)"
  attempt=0

  while true; do
    attempt=$((attempt + 1))

    set +e
    probe "$kind" "$rest"
    probe_status=$?
    set -e

    if [ "$probe_status" -eq 0 ]; then
      echo "wait-for-services: $spec is ready (attempt ${attempt})"
      return 0
    fi

    # Exit code 2 means the spec itself is bad; retrying will never help.
    if [ "$probe_status" -eq 2 ]; then
      return 2
    fi

    elapsed=$(($(date +%s) - started_at))
    if [ "$elapsed" -ge "$timeout_seconds" ]; then
      echo "wait-for-services: timed out after ${elapsed}s waiting for $spec" >&2
      return 1
    fi

    sleep "$interval_seconds"
  done
}

main() {
  if [ "$#" -eq 0 ]; then
    usage
    return 2
  fi

  for service_spec in "$@"; do
    wait_for_one "$service_spec"
  done

  echo "wait-for-services: all services ready"
}

main "$@"
