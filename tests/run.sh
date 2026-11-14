#!/bin/sh
# Run the offline test suite. Proxy variables are dropped so the fake webhook on 127.0.0.1 is reached directly.
cd "$(dirname "$0")/.." || exit 1
exec env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    python3 -W error::ResourceWarning -m unittest discover -s tests -v "$@"
