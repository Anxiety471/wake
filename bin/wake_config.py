"""Shared settings for the wake scripts.

Every setting is read from the environment first, then from a KEY=VALUE file
($WAKE_CONFIG, default $WAKE_CONFIG_DIR/config.env, default ~/.config/wake/config.env),
then falls back to the default below. An empty value in the environment counts as set.
"""
import os

DEFAULTS = {
    "WAKE_API_BASE": "",                      # e.g. https://bots.example.com/api/v1/bots ; POST {base}/{bot-id}/webhook
    "WAKE_STATE_DIR": "/var/lib/wake",        # shared threads/ and inbox/ live here (on the host)
    "WAKE_HOSTOPS": "",                       # executable: runs its one argument as a shell command on the host, stdin passed through
    "WAKE_KEY_HELPER": "wake-webhook-key",    # creates/rotates webhook keys: has-key ID | ID [--rotate] | enable-bot ID
    "WAKE_GROUP": "wake",                     # group that may read key files; used by the `sg` fallback
    "WAKE_REGISTRY_GROUP": "sudo",            # group given to the host copy of the registry by --sync
    "WAKE_RATE_DIR": "/tmp",                  # where the per-uid rate log wake-rate-<uid>.json is kept
    "WAKE_BOT": "coordinator",                # default bot for wake-hook, wake-run and the legacy `wake <source> <event>` form
}


def config_dir(environ=None):
    env = os.environ if environ is None else environ
    return os.path.expanduser(env.get("WAKE_CONFIG_DIR") or "~/.config/wake")


def parse_env_file(text):
    """KEY=VALUE lines; blank lines and # comments ignored; one layer of quotes stripped."""
    vals = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        vals[k] = v
    return vals


def load(environ=None):
    """Resolved settings as a dict, including the derived paths WAKE_CONFIG_DIR, WAKE_KEY_DIR and WAKE_REGISTRY."""
    env = os.environ if environ is None else environ
    cdir = config_dir(env)
    path = os.path.expanduser(env.get("WAKE_CONFIG") or os.path.join(cdir, "config.env"))
    try:
        with open(path) as f:
            filevals = parse_env_file(f.read())
    except OSError:
        filevals = {}

    def get(k, default):
        if k in env:
            return env[k]
        return filevals.get(k, default)

    cfg = {k: get(k, d) for k, d in DEFAULTS.items()}
    cfg["WAKE_CONFIG_DIR"] = cdir
    cfg["WAKE_KEY_DIR"] = os.path.expanduser(get("WAKE_KEY_DIR", cdir))
    cfg["WAKE_REGISTRY"] = os.path.expanduser(get("WAKE_REGISTRY", os.path.join(cdir, "bots.tsv")))
    cfg["WAKE_STATE_DIR"] = os.path.expanduser(cfg["WAKE_STATE_DIR"]).rstrip("/") or "/"
    cfg["WAKE_HOSTOPS"] = os.path.expanduser(cfg["WAKE_HOSTOPS"])
    return cfg
