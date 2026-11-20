"""Shared helpers of the swap tracker (2026-11-20): the parts of the research project's python/collectors/common.py that the collector needs,
with the log and lock folders taken from the settings file (collector_settings.py). Linux only.
  ROOT, DATA         repository root (works in the private layout and in the public python/collectors layout) and <root>/data
  setup_logging(n)   rotating file log <logs_dir>/<n>.log (5 MB x 5), console too when attached
  single_instance(n) heartbeat lock <locks_dir>/<n>.json shared with the C++ runtime (host, pid, updated); exits quietly if another live
                     instance holds it; SIGTERM becomes a normal exit so the lock is released"""
import atexit, json, logging, logging.handlers, os, socket, sys, threading, time
from datetime import datetime, timezone

from collector_settings import ROOT, S as SETTINGS

DATA = os.path.join(ROOT, "data")
LOCK_DIR = SETTINGS.locks_dir
LOCK_STALE_S = 75
IN_DOCKER = os.path.exists("/.dockerenv")


def utcnow():
    return datetime.now(timezone.utc)


def setup_logging(name):
    """Rotating file log (5 MB x 5) in the logs folder; also echoes to the console when one is attached."""
    os.makedirs(SETTINGS.logs_dir, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
    handlers = [logging.handlers.RotatingFileHandler(os.path.join(SETTINGS.logs_dir, f"{name}.log"), maxBytes=5_000_000, backupCount=5, encoding="utf8")]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger(name)


def _lock_owner():
    return {"host": socket.gethostname(), "pid": os.getpid(), "docker": IN_DOCKER}


def single_instance(name):
    """Exit quietly if another copy of this process is already running ANYWHERE that writes this data folder
    (prevents duplicate collectors doubling request rates and interleaving file writes).
      * Windows: a named mutex (same login session), as before.
      * Everywhere: a heartbeat file data/locks/<name>.json refreshed every 20 s. It is shared by the Windows and the Docker
        copies through the bind mount, so a Docker collector and a leftover Windows collector can never both run.
        A heartbeat older than 75 s is stale (crash); on Linux a heartbeat from this same host whose pid is gone is stale at once
        (a restarted container), and the file is removed on a clean exit."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = os.path.join(LOCK_DIR, f"{name}.json")
    me = _lock_owner()
    try:
        cur = json.load(open(path))
        age = time.time() - float(cur.get("updated", 0))
        same = cur.get("host") == me["host"] and cur.get("pid") == me["pid"]
        dead_local = False
        if os.name != "nt" and cur.get("host") == me["host"] and not same:
            try:
                os.kill(int(cur["pid"]), 0)          # POSIX only: signal 0 = existence check (never call this on Windows)
            except (ProcessLookupError, ValueError, KeyError):
                dead_local = True
            except PermissionError:
                pass
        if not same and age < LOCK_STALE_S and not dead_local:
            print(f"{name}: another instance holds {path} ({cur.get('host')} pid {cur.get('pid')}, {age:.0f} s ago) - exiting", file=sys.stderr)
            sys.exit(0)
    except (OSError, ValueError):
        pass

    def beat():
        while True:
            try:
                tmp = path + f".{me['pid']}.tmp"
                with open(tmp, "w") as f:
                    json.dump({**me, "updated": time.time(), "updated_iso": utcnow().isoformat(timespec="seconds")}, f)
                os.replace(tmp, path)
            except OSError:
                pass
            time.sleep(20)

    def release():
        try:
            cur = json.load(open(path))
            if cur.get("host") == me["host"] and cur.get("pid") == me["pid"]:
                os.remove(path)
        except (OSError, ValueError):
            pass

    threading.Thread(target=beat, daemon=True, name="instance-lock").start()
    atexit.register(release)
    # `docker stop` / the runner send SIGTERM; Python's default handler dies WITHOUT running atexit, which left the lock behind and
    # made the next container wait out LOCK_STALE_S (found 2026-11-14). Turn SIGTERM into a normal exit so release() runs.
    if threading.current_thread() is threading.main_thread():
        import signal
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
