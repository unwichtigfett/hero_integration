"""Configure the Dev site's RQ topology on startup, including after a snapshot restore."""
from pathlib import Path
import fcntl
import json
import os
import tempfile


def configure(path):
    with path.with_suffix(".hero-worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conf = json.loads(path.read_text())
        desired = {"timeout": 1500, "background_workers": 0}
        workers = conf.setdefault("workers", {})
        if (workers.get("hero_binding") == desired
                and conf.get("hero_migration_workers") == 10
                and conf.get("hero_shared_workers") == 10
                and conf.get("hero_native_migration_timeout_seconds") == 21600):
            return
        workers["hero_binding"] = desired
        conf["hero_migration_workers"] = 10
        conf["hero_shared_workers"] = 10
        conf["hero_native_migration_timeout_seconds"] = 21600
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as output:
            temporary = Path(output.name)
            try:
                os.fchmod(output.fileno(), path.stat().st_mode & 0o777)
                json.dump(conf, output, indent=1)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    configure(Path("sites/common_site_config.json"))
