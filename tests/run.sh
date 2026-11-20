#!/bin/sh
# Run the offline test suite. Proxy variables are dropped so the fake webhook on 127.0.0.1 is reached directly.
# WAKE_SYSTEM_CONFIG is set empty so a machine that has /etc/wake/config.env does not decide what the
# tests see: a site file carrying WAKE_MCP_TOKEN used to fail three bridge tests (#135 F5).
#   tests/run.sh                    # python3 from PATH
#   PYTHON=python3.9 tests/run.sh   # another interpreter
# CI also runs the suite as a background job (.github/workflows/ci.yml), where POSIX gives
# it SIGINT ignored; a test that depends on the foreground disposition fails there.
cd "$(dirname "$0")/.." || exit 1
PYTHON=${PYTHON:-python3}
if [ "$(id -u)" = 0 ]; then
    echo "note: running as root; the sg fallback tests will be skipped (root ignores file modes). Run as a non-root user to cover them." >&2
fi
exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    WAKE_SYSTEM_CONFIG= \
    PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON" -W error::ResourceWarning -m unittest discover -s tests -v "$@"
