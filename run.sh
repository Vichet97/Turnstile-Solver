#!/bin/bash

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

if id "root" &>/dev/null; then
    echo "root:root" | chpasswd || {
        echo "Failed to update password, continuing..."
    }
else
    if ! getent group root >/dev/null; then
        addgroup root
    fi

    useradd -m -s /bin/bash -g root root || {
        echo "Failed to create user, continuing..."
    }
    echo "root:root" | chpasswd || {
        echo "Failed to set password, continuing..."
    }
    usermod -aG sudo root || {
        echo "Failed to add user to sudo, continuing..."
    }
fi

if [ -n "$TZ" ]; then
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime
    echo $TZ >/etc/timezone
fi

if [ "$RUN_API_SOLVER" = "true" ]; then
    echo "Starting API solver with virtual display..."
    xvfb-run -a python api_solver.py --browser_type chrome --host 0.0.0.0
else
    trap "stop_xrdp_services" SIGKILL SIGTERM SIGHUP SIGINT EXIT
    start_xrdp_services
    # Keep container running
    tail -f /dev/null
fi
