#!/bin/sh
# Development-only entrypoint. The ingest service already owns the token, so
# checking it here avoids copying secret material into a separate preflight
# container (and therefore avoids another `docker inspect` exposure).

set -eu

is_loopback() {
    case "${1:-}" in
        127.0.0.1|localhost|::1|'[::1]') return 0 ;;
        *) return 1 ;;
    esac
}

token=${INGEST_AUTH_TOKEN:-}
debug_bind_host=${DEV_DEBUG_BIND_HOST:-127.0.0.1}
robot_bind_host=${DEV_ROBOT_BIND_HOST:-127.0.0.1}
profile=${INGEST_SECURITY_PROFILE:-dev}

if [ "$profile" = dev ] && [ "$token" = dev-ingest-token-change-me ]; then
    if ! is_loopback "$debug_bind_host" || ! is_loopback "$robot_bind_host"; then
        echo 'ERROR: refusing non-loopback dev ingest/robot exposure with the committed fallback INGEST_AUTH_TOKEN.' >&2
        echo '       Set INGEST_AUTH_TOKEN to a random secret before widening DEBUG_BIND_HOST or ROBOT_BIND_HOST.' >&2
        exit 42
    fi
    echo 'WARNING: dev ingest is using the committed fallback INGEST_AUTH_TOKEN; keep ingest and the robot UI loopback-only.' >&2
fi

exec "$@"
