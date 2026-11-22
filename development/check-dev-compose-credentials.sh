#!/bin/sh
# Guard the development Compose shortcuts that can publish credential-bearing
# services.  This script deliberately receives only configuration booleans and
# bind hosts: secrets are already mounted in their owning services and must not
# be copied into a guard container (where `docker inspect` would expose them).

set -eu

is_loopback() {
    case "${1:-}" in
        127.0.0.1|localhost|::1|'[::1]') return 0 ;;
        *) return 1 ;;
    esac
}

scope=${DEV_GUARD_SCOPE:-ingest}
debug_bind_host=${DEV_DEBUG_BIND_HOST:-127.0.0.1}
robot_bind_host=${DEV_ROBOT_BIND_HOST:-127.0.0.1}
token_set=${DEV_INGEST_AUTH_TOKEN_SET:-}
query_bind_host=${DEV_QUERY_BIND_HOST:-127.0.0.1}
ui_bind_host=${DEV_UI_BIND_HOST:-127.0.0.1}
query_oidc_dev=${DEV_QUERY_OIDC_DEV:-false}
query_unsafe_allowed=${DEV_QUERY_UNSAFE_EXPOSURE_ALLOWED:-}

case "$scope" in
    ingest)
        # The robot UI has no login of its own and the debug overlay publishes
        # ingest's gRPC/admin listeners.  A non-loopback binding therefore must
        # never run with the committed/implicit shared token.
        if { ! is_loopback "$debug_bind_host" || ! is_loopback "$robot_bind_host"; } \
            && [ "$token_set" != true ]; then
            echo 'ERROR: refusing non-loopback dev ingest/robot exposure without an explicit INGEST_AUTH_TOKEN.' >&2
            echo '       Set INGEST_AUTH_TOKEN to a random secret before widening DEBUG_BIND_HOST or ROBOT_BIND_HOST.' >&2
            exit 42
        fi
        if [ "$token_set" = true ] \
            && { ! is_loopback "$debug_bind_host" || ! is_loopback "$robot_bind_host"; }; then
            echo 'WARNING: dev ingest/robot exposure is non-loopback; the robot UI has no independent login. Restrict the network and rotate the token if it leaks.' >&2
        fi
        ;;
    storage)
        # The development Postgres service has a fixed, well-known password;
        # the object store has a committed development root password.  There is no safe way to
        # publish this pair to a LAN from this Compose file, so fail closed.
        if ! is_loopback "$debug_bind_host"; then
            echo 'ERROR: refusing non-loopback dev Postgres/object-store publication.' >&2
            echo '       The development Postgres password is fixed; use make expose-ingest for an off-box robot instead of make debug-up.' >&2
            exit 43
        fi
        ;;
    query)
        if ! is_loopback "$query_bind_host" || ! is_loopback "$ui_bind_host"; then
            if [ "$query_oidc_dev" = true ] && [ "$query_unsafe_allowed" != 1 ]; then
                echo 'ERROR: refusing non-loopback dev query/UI exposure while OIDC_DEV can mint an admin token.' >&2
                echo '       Keep QUERY_BIND_HOST and UI_BIND_HOST on loopback, or set ALLOW_UNSAFE_DEV_QUERY_EXPOSURE=1 for a trusted, firewalled network.' >&2
                exit 45
            fi
            echo 'WARNING: query/UI is exposed beyond loopback.' >&2
            if [ "$query_oidc_dev" = true ]; then
                echo 'WARNING: OIDC_DEV is enabled and /dev/token can mint an admin token. Do not use this setting on an untrusted network.' >&2
            fi
        fi
        ;;
    *)
        echo "ERROR: unknown development credential guard scope: $scope" >&2
        exit 44
        ;;
esac

exit 0
