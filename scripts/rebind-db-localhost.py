#!/usr/bin/env python3
"""Recreates a database container started with `docker run`, publishing the port only on 127.0.0.1.

The binding of an existing container cannot be changed: it must be recreated. To avoid losing data:
  checks (refuses if it cannot recreate faithfully) -> measure -> clean stop -> volume backup (tar, 0600)
  -> rename the old one -> recreate with the same volume -> verify -> remove the old one.
If ANY step fails after the stop, the original container is restored and restarted.
Environment variables (passwords) go through a 0600 temporary file, never through the command line.

Usage: rebind-db-localhost.py NAME HOST_PORT CONTAINER_PORT BACKUP_DIR(absolute)
Exit codes: 0 ok · 1 failed and restored · 2 refused (nothing was touched)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

EXTRA_HOSTCONFIG = ("Memory", "MemorySwap", "NanoCpus", "CpuShares", "CpuQuota", "Privileged", "PidMode", "ReadonlyRootfs", "Init",
                    "CapAdd", "CapDrop", "Dns", "ExtraHosts", "Ulimits", "SecurityOpt", "Devices", "Tmpfs", "Sysctls", "Links",
                    "VolumesFrom")


def sh(*args, check=True, **kw):
    r = subprocess.run(list(args), capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}...: {r.stderr.strip()[:300]}")
    return r


def inspect(name):
    return json.loads(sh("docker", "inspect", name).stdout)[0]


def unsupported(d, img):
    """Reasons why the container CANNOT be recreated faithfully by this script. Empty list = it can.

    The script only recreates: image, env, one volume, restart policy and one port. Everything else (command, entrypoint,
    user, other mounts, network, healthcheck, limits...) would be silently lost: better to refuse before touching anything.
    """
    c, h, ic = d["Config"], d["HostConfig"], img["Config"]
    why = []
    for key in ("Cmd", "Entrypoint", "Healthcheck"):
        if c.get(key) != ic.get(key):
            why.append(f"{key} differs from the image's")
    if c.get("User"):
        why.append("User is set")
    if (c.get("WorkingDir") or "") != (ic.get("WorkingDir") or ""):
        why.append("WorkingDir differs from the image's")
    if {k for k in (c.get("Labels") or {})} - {k for k in (ic.get("Labels") or {})}:
        why.append("labels added")
    mounts = d.get("Mounts") or []
    if len(mounts) != 1 or mounts[0]["Type"] != "volume":
        why.append("mounts are not a single volume")
    else:
        # a volume given with -v NAME:PATH also appears in Binds and can be recreated identically;
        # host paths, read-only or other options cannot
        ok = {f"{mounts[0]['Name']}:{mounts[0]['Destination']}", f"{mounts[0]['Name']}:{mounts[0]['Destination']}:rw"}
        if any(b not in ok for b in h.get("Binds") or []):
            why.append("Binds other than the single volume")
    if h.get("NetworkMode") not in ("bridge", "default"):
        why.append(f"network {h.get('NetworkMode')} (not bridge)")
    why += [f"{k} is set" for k in EXTRA_HOSTCONFIG if h.get(k)]
    if h.get("ShmSize") not in (None, 0, 67108864):
        why.append("ShmSize is not the default")
    if (h.get("LogConfig") or {}).get("Type") != "json-file" or (h.get("LogConfig") or {}).get("Config"):
        why.append("log driver is not the default")
    if (h.get("RestartPolicy") or {}).get("MaximumRetryCount"):
        why.append("restart on-failure with a maximum retry count")
    pb = h.get("PortBindings") or {}
    if len(pb) != 1 or any(len(v or []) != 1 for v in pb.values()):
        why.append("not exactly one published port")
    if any("\n" in e for e in c.get("Env") or []):
        why.append("an environment variable contains a newline")
    return why


def probe(name, kind):
    """Data state, comparable before/after: number of user tables (postgres) or keys (redis)."""
    if kind == "redis":
        return sh("docker", "exec", name, "redis-cli", "DBSIZE").stdout.strip()
    q = "select count(*) || ' tables' from pg_tables where schemaname not in ('pg_catalog','information_schema')"
    return sh("docker", "exec", name, "sh", "-c", f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{q}"').stdout.strip()


def wait_ready(name, kind, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        cmd = ["redis-cli", "ping"] if kind == "redis" else ["pg_isready", "-q"]
        if sh("docker", "exec", name, *cmd, check=False).returncode == 0:
            return True
        time.sleep(2)
    return False


def backup_volume(image, vol, backup_dir, tgz, kind):
    """Copies the volume into a 0600 tar.gz and checks it is readable. Raises if anything looks wrong."""
    path = os.path.join(backup_dir, tgz)
    uid, gid = os.getuid(), os.getgid()
    script = f"tar czf /backup/{tgz} -C /data . && chown {uid}:{gid} /backup/{tgz} && chmod 600 /backup/{tgz}"
    sh("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{vol}:/data:ro", "-v", f"{backup_dir}:/backup", image, "-c", script)
    listing = sh("tar", "tzf", path).stdout.splitlines()
    if len(listing) < 2 or os.path.getsize(path) < 100:
        raise RuntimeError(f"suspicious backup ({len(listing)} entries)")
    if kind == "postgres" and not any(x.rstrip("/").endswith("PG_VERSION") for x in listing):
        raise RuntimeError("PG_VERSION is missing from the backup: it does not look like a postgres cluster")
    return path, len(listing)


def rollback(name, old, st):
    """Puts everything back as it was: removes the new one, gives the name back to the old one and restarts it."""
    if st["new"]:
        sh("docker", "rm", "-f", name, check=False)
    if st["renamed"]:
        sh("docker", "rename", old, name, check=False)
    if st["stopped"]:
        sh("docker", "start", name, check=False)
    return sh("docker", "inspect", "-f", "{{.State.Status}}", name, check=False).stdout.strip()


def main(name, host_port, cport, backup_dir):
    if not os.path.isabs(backup_dir):
        print("BACKUP_DIR must be an absolute path", file=sys.stderr)
        return 2
    d = inspect(name)
    image = d["Config"]["Image"]
    kind = "redis" if "redis" in image.rsplit("/", 1)[-1] else "postgres"
    img = json.loads(sh("docker", "image", "inspect", image).stdout)[0]
    why = unsupported(d, img)
    if img["Id"] != d["Image"]:  # the tag was pulled again: another version on the old volume may corrupt it
        why.append("the image tag now points to another image (ID differs from the container's)")
    if why:
        print(f"[{name}] REFUSED, nothing was touched: " + "; ".join(why), file=sys.stderr)
        return 2
    vol, dest = d["Mounts"][0]["Name"], d["Mounts"][0]["Destination"]
    restart = (d["HostConfig"]["RestartPolicy"] or {}).get("Name") or "no"
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    # owner-only on purpose: backups may contain database data
    os.chmod(backup_dir, 0o700)  # nosemgrep
    size_kb = int(sh("docker", "run", "--rm", "--entrypoint", "du", "-v", f"{vol}:/data:ro", image, "-sk", "/data").stdout.split()[0])
    if shutil.disk_usage(backup_dir).free < 2 * size_kb * 1024:
        print(f"[{name}] REFUSED: not enough space for the backup ({size_kb} KiB to save)", file=sys.stderr)
        return 2
    tgz = f"{name}-{vol[:12]}-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
    if os.path.exists(os.path.join(backup_dir, tgz)):
        print(f"[{name}] REFUSED: {tgz} already exists (a backup is never overwritten)", file=sys.stderr)
        return 2

    before = probe(name, kind)
    print(f"[{name}] before: {before}")
    old = f"{name}-old-rebind"
    st = {"stopped": False, "renamed": False, "new": False}
    envfile = None
    try:
        sh("docker", "stop", "-t", "60", name)
        st["stopped"] = True
        path, n = backup_volume(image, vol, backup_dir, tgz, kind)
        print(f"[{name}] backup ok: {path} ({n} entries)")
        sh("docker", "rename", name, old)
        st["renamed"] = True
        fd, envfile = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(d["Config"]["Env"]) + "\n")
        st["new"] = True  # from here a container with this name may exist even if `run` fails halfway
        sh("docker", "run", "-d", "--name", name, "--restart", restart, "-p", f"127.0.0.1:{host_port}:{cport}",
           "--env-file", envfile, "-v", f"{vol}:{dest}", image)
        if not wait_ready(name, kind):
            raise RuntimeError("the new container does not become ready within 90 s")
        after = probe(name, kind)
        binding = sh("docker", "port", name).stdout.strip()
        print(f"[{name}] after: {after} | port: {binding.replace(chr(10), ' ; ')}")
        if after != before:
            raise RuntimeError(f"data mismatch: before '{before}', after '{after}'")
        if f"127.0.0.1:{host_port}" not in binding or "0.0.0.0" in binding:
            raise RuntimeError(f"the binding is not only on 127.0.0.1: {binding}")
    except Exception as e:  # noqa: BLE001 - any error after the stop: restore
        print(f"[{name}] ERROR: {e}", file=sys.stderr)
        state = rollback(name, old, st)
        print(f"[{name}] restored the original container: state '{state}' (backup in {backup_dir})", file=sys.stderr)
        return 1
    finally:
        if envfile and os.path.exists(envfile):
            os.unlink(envfile)
    if sh("docker", "rm", old, check=False).returncode != 0:  # without -v: the volume is used by the new container
        print(f"[{name}] warning: could not remove {old}; remove it by hand (docker rm {old})", file=sys.stderr)
    print(f"[{name}] OK: recreated on 127.0.0.1:{host_port}, identical data, old container removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]))
