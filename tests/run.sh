#!/bin/sh
# Run the offline test suite. Proxy variables are dropped so the fake webhook on 127.0.0.1 is reached directly.
#   tests/run.sh                    # python3 from PATH
#   PYTHON=python3.9 tests/run.sh   # another interpreter
cd "$(dirname "$0")/.." || exit 1
PYTHON=${PYTHON:-python3}
if [ "$(id -u)" = 0 ]; then
    echo "note: running as root; the two sg fallback tests will be skipped (root ignores file modes). Run as a non-root user to cover them." >&2
fi
exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    "$PYTHON" -W error::ResourceWarning -m unittest discover -s tests -v "$@"
