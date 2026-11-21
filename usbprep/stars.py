"""Star = the user's explicit "this IS a dedicated USB drive" mark for a USB drive that detection missed.

Rules (see docs/README.md, "Star"):
- Stored by stable identity (serial + model + vendor + size [+ USB vendor:product id]), NEVER by /dev path,
  in <config_dir>/stars.json (0600, atomic write; a corrupt file is treated as empty).
- A drive without a serial number cannot be pinned, so it is never saved: only a session-only star exists,
  passed by the GUI (or a shell) in the USBPREP_SESSION_STARS environment variable (JSON list of keys).
- The star overrides only the flash-vs-not classification. Hard refusals (not USB / unknown bus, system or
  boot disk, mounted host system partition, no medium, too small) are never overridden.
"""
import json
import os
import tempfile
import time

ENV_SESSION = "USBPREP_SESSION_STARS"
STAR_REASON = "manually starred by the user as a dedicated USB drive (overrides flash-drive detection only)"
NO_SERIAL = ("no serial number reported: the drive cannot be identified reliably after re-plugging, so the star is "
             "NOT saved; only a session-only star is possible")


class StarError(RuntimeError):
    pass


def stars_path(osname=None) -> str:
    from .backends import config_dir
    return os.path.join(config_dir(osname), "stars.json")


def identity_key(dev):
    """Persistent key, or None when the drive has no serial (cannot be pinned)."""
    if not (dev.serial or "").strip():
        return None
    usb_id = (dev.raw or {}).get("usb_id", "") if isinstance(dev.raw, dict) else ""
    return "|".join([dev.serial.strip(), dev.model, dev.vendor, str(dev.size), usb_id])


def session_key(dev) -> str:
    return "|".join(["session", dev.path, dev.model, dev.vendor, str(dev.size)])


def star_refusal(dev):
    """None if the drive may be starred, else why not (hard protections; never overridable)."""
    hard = getattr(dev, "hard_reasons", None)
    if hard:
        return "cannot be starred: " + "; ".join(hard)
    if dev.transport != "usb":
        return "cannot be starred: bus is not USB or cannot be determined"
    return None


def load(osname=None) -> list:
    try:
        with open(stars_path(osname), encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("stars", []) if isinstance(data, dict) else []
        return [x for x in items if isinstance(x, dict) and isinstance(x.get("key"), str)]
    except (OSError, ValueError, AttributeError):
        return []


def save(entries, osname=None) -> str:
    path = stars_path(osname)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".stars-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "stars": entries}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def session_keys() -> set:
    try:
        v = json.loads(os.environ.get(ENV_SESSION) or "[]")
        return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()
    except ValueError:
        return set()


def star_state(dev, saved=None) -> str:
    """'saved', 'session' or '' (identity must match exactly; the /dev path alone never matches)."""
    key = identity_key(dev)
    if key and key in {e["key"] for e in (load() if saved is None else saved)}:
        return "saved"
    if session_key(dev) in session_keys() and not key:
        return "session"
    return ""


def apply(dev):
    """Set dev.status/reasons from the detection result plus the star (hard refusals always win)."""
    soft, amb, sticky = dev.soft_reasons, dev.amb_reasons, dev.sticky_reasons
    dev.starred = "" if star_refusal(dev) else star_state(dev)
    if dev.starred:
        dev.status = "ambiguous" if sticky else "eligible"
        dev.reasons = ([STAR_REASON + (" - this session only" if dev.starred == "session" else "")] + sticky
                       + [f"detection said: {r}" for r in soft + amb])
    return dev


def star(dev, osname=None) -> str:
    why = star_refusal(dev)
    if why:
        raise StarError(f"{dev.path}: {why}")
    key = identity_key(dev)
    if not key:
        raise StarError(f"{dev.path}: {NO_SERIAL}")
    entries = [e for e in load(osname) if e["key"] != key]
    entries.append({"key": key, "serial": dev.serial, "model": dev.model, "vendor": dev.vendor, "size": dev.size,
                    "path_when_starred": dev.path, "starred_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return save(entries, osname)


def unstar(target, devices=(), osname=None) -> list:
    """Remove saved marks matching a device path (by its CURRENT identity), a key, or a serial. Returns removed."""
    keys = {target}
    for d in devices:
        if d.path == target and identity_key(d):
            keys.add(identity_key(d))
    entries = load(osname)
    removed = [e for e in entries if e["key"] in keys or e.get("serial") == target]
    if removed:
        save([e for e in entries if e not in removed], osname)
    return removed
