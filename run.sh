#!/bin/bash

set -euo pipefail

CURRENT_UID="$(id -u)"
IS_ROOT=0
if [ "$CURRENT_UID" -eq 0 ]; then
    IS_ROOT=1
fi

start_xrdp_services() {
    rm -rf /var/run/xrdp-sesman.pid
    rm -rf /var/run/xrdp.pid
    rm -rf /var/run/xrdp/xrdp-sesman.pid
    rm -rf /var/run/xrdp/xrdp.pid

    xrdp-sesman &
    xrdp -n &

    echo "Waiting for X server to be ready..."
    for i in {1..20}; do
        if pgrep Xorg >/dev/null; then
            echo "Xorg is running."
            return
        fi
        sleep 1
    done

    echo "Xorg not detected after timeout."
}

stop_xrdp_services() {
    xrdp --kill
    xrdp-sesman --kill
    exit 0
}

configure_root_password() {
    if [ "$IS_ROOT" -ne 1 ]; then
        echo "Non-root runtime (uid=$CURRENT_UID): skipping root password setup."
        return
    fi
    if id "root" &>/dev/null; then
        echo "root:root" | chpasswd || {
            echo "Failed to update root password, continuing..."
        }
    else
        if ! getent group root >/dev/null; then
            addgroup root || true
        fi
        useradd -m -s /bin/bash -g root root || {
            echo "Failed to create root user, continuing..."
        }
        echo "root:root" | chpasswd || {
            echo "Failed to set root password, continuing..."
        }
        usermod -aG sudo root || {
            echo "Failed to add root user to sudo group, continuing..."
        }
    fi
}

configure_timezone() {
    if [ -z "${TZ:-}" ]; then
        return
    fi
    if [ ! -e "/usr/share/zoneinfo/$TZ" ]; then
        echo "Timezone '$TZ' not found under /usr/share/zoneinfo; keeping environment TZ only."
        return
    fi
    if [ "$IS_ROOT" -ne 1 ]; then
        echo "Non-root runtime (uid=$CURRENT_UID): cannot write /etc/localtime or /etc/timezone; using TZ=$TZ environment only."
        return
    fi
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" >/etc/timezone
}

prefetch_seleniumbase_drivers() {
    local prefetch_raw prefetch_lc browser_lc uc_raw uc_lc headless_raw headless_lc uc_enabled
    prefetch_raw="${SELENIUMBASE_PREFETCH_DRIVER:-true}"
    prefetch_lc="$(echo "$prefetch_raw" | tr '[:upper:]' '[:lower:]')"
    case "$prefetch_lc" in
        0|false|no|off)
            echo "SeleniumBase driver prefetch disabled via SELENIUMBASE_PREFETCH_DRIVER=$prefetch_raw"
            return
            ;;
    esac

    browser_lc="$(echo "${SOLVER_BROWSER_TYPE:-seleniumbase}" | tr '[:upper:]' '[:lower:]')"
    case "$browser_lc" in
        seleniumbase|selenium|sb)
            ;;
        *)
            return
            ;;
    esac

    if ! command -v sbase >/dev/null 2>&1; then
        echo "sbase command not found; skipping SeleniumBase driver prefetch."
        return
    fi

    echo "Prefetching SeleniumBase chromedriver..."
    sbase get chromedriver || echo "Warning: chromedriver prefetch failed; runtime fallback download remains enabled."

    uc_raw="${SELENIUMBASE_UC:-}"
    uc_lc="$(echo "$uc_raw" | tr '[:upper:]' '[:lower:]')"
    headless_raw="${SOLVER_HEADLESS:-false}"
    headless_lc="$(echo "$headless_raw" | tr '[:upper:]' '[:lower:]')"
    uc_enabled="false"
    case "$uc_lc" in
        1|true|yes|on)
            uc_enabled="true"
            ;;
        0|false|no|off)
            uc_enabled="false"
            ;;
        *)
            # Default OFF unless explicitly enabled via SELENIUMBASE_UC=true.
            uc_enabled="false"
            ;;
    esac

    if [ "$uc_enabled" = "true" ]; then
        echo "Prefetching SeleniumBase uc_driver..."
        sbase get uc_driver || echo "Warning: uc_driver prefetch failed; runtime uc=False fallback remains enabled."
    fi
}

configure_root_password
configure_timezone

if [ "${RUN_API_SOLVER:-false}" = "true" ]; then
    echo "Starting API solver with virtual display..."
    ENABLE_RDP_WITH_API="${ENABLE_RDP_WITH_API:-false}"
    SOLVER_BROWSER_TYPE="${SOLVER_BROWSER_TYPE:-seleniumbase}"
    SOLVER_THREAD="${SOLVER_THREAD:-1}"
    XVFB_SCREEN_WIDTH="${XVFB_SCREEN_WIDTH:-1920}"
    XVFB_SCREEN_HEIGHT="${XVFB_SCREEN_HEIGHT:-1080}"
    XVFB_SCREEN_DEPTH="${XVFB_SCREEN_DEPTH:-24}"
    XVFB_DPI="${XVFB_DPI:-96}"
    XVFB_SERVER_ARGS="-screen 0 ${XVFB_SCREEN_WIDTH}x${XVFB_SCREEN_HEIGHT}x${XVFB_SCREEN_DEPTH} -dpi ${XVFB_DPI} -ac +extension RANDR -noreset"

    echo "Xvfb geometry: ${XVFB_SCREEN_WIDTH}x${XVFB_SCREEN_HEIGHT}x${XVFB_SCREEN_DEPTH} @ ${XVFB_DPI} DPI"
    prefetch_seleniumbase_drivers

    if [ "$ENABLE_RDP_WITH_API" = "true" ]; then
        if [ "$IS_ROOT" -ne 1 ]; then
            echo "ENABLE_RDP_WITH_API=true requested, but current user is non-root (uid=$CURRENT_UID). XRDP will not start."
        else
            echo "ENABLE_RDP_WITH_API=true: starting XRDP services alongside API."
            start_xrdp_services
        fi
    fi

    SOLVER_HEADLESS_ARGS=""
    if [ "${SOLVER_HEADLESS:-false}" = "true" ]; then
        SOLVER_HEADLESS_ARGS="--headless"
    fi
    SOLVER_DEBUG_ARGS=""
    if [ "${SOLVER_DEBUG:-false}" = "true" ]; then
        SOLVER_DEBUG_ARGS="--debug"
    fi

    API_CMD=(xvfb-run -a --error-file=/tmp/xvfb-errors.log --server-args="$XVFB_SERVER_ARGS" python api_solver.py --browser_type "$SOLVER_BROWSER_TYPE" --thread "$SOLVER_THREAD" --host 0.0.0.0)
    if [ -n "$SOLVER_HEADLESS_ARGS" ]; then
        API_CMD+=("$SOLVER_HEADLESS_ARGS")
    fi
    if [ -n "${SOLVER_USERAGENT:-}" ]; then
        API_CMD+=(--useragent "$SOLVER_USERAGENT")
    fi
    if [ -n "$SOLVER_DEBUG_ARGS" ]; then
        API_CMD+=("$SOLVER_DEBUG_ARGS")
    fi

    "${API_CMD[@]}"
else
    if [ "$IS_ROOT" -ne 1 ]; then
        echo "RUN_API_SOLVER=false requires root privileges for XRDP/Xorg startup. Run container as root."
        exit 1
    fi
    trap "stop_xrdp_services" SIGKILL SIGTERM SIGHUP SIGINT EXIT
    start_xrdp_services
    # Keep container running
    tail -f /dev/null
fi
