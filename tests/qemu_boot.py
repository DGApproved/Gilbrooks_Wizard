"""QEMU helpers: boot a disk IMAGE (never a device) under SeaBIOS or OVMF, read the greeting, send Enter,
and detect the reboot (-no-reboot makes QEMU exit on guest reset)."""
import os
import shutil
import socket
import subprocess
import time

OVMF = next((p for p in ("/usr/share/qemu/OVMF.fd", "/usr/share/ovmf/OVMF.fd") if os.path.isfile(p)), None)
QEMU = shutil.which("qemu-system-x86_64")


def _hmp(sock_path, cmd, wait=0.5):
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock_path)
    s.settimeout(2)
    buf = b""
    try:
        time.sleep(0.2)
        buf += s.recv(65536)
        s.sendall(cmd.encode() + b"\n")
        time.sleep(wait)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return buf.decode(errors="replace")


def vga_text(sock_path):
    out = _hmp(sock_path, "xp /2000hx 0xb8000", 1.5)
    chars = []
    for line in out.splitlines():
        if ":" in line and line.strip().startswith("00000000000b8"):
            for w in line.split(":", 1)[1].split():
                if w.startswith("0x"):
                    c = int(w, 16) & 0xFF
                    chars.append(chr(c) if 32 <= c < 127 else " ")
    return "".join(chars)


def boot(image, firmware, workdir, greeting_timeout=90):
    """Return dict(greeting=bool, rebooted=bool, screen/serial excerpt, firmware=str)."""
    sock = os.path.join(workdir, "mon.sock")
    serial = os.path.join(workdir, "serial.log")
    for p in (sock, serial):
        if os.path.exists(p):
            os.remove(p)
    argv = [QEMU, "-m", "256", "-display", "none", "-no-reboot", "-nodefaults", "-vga", "std",
            "-drive", f"file={image},format=raw,if=ide,snapshot=on",
            "-monitor", f"unix:{sock},server,nowait", "-serial", f"file:{serial}"]
    if firmware == "ovmf":
        argv += ["-bios", OVMF]
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    res = {"firmware": firmware, "greeting": False, "rebooted": False, "text": ""}
    try:
        deadline = time.time() + greeting_timeout
        while time.time() < deadline and p.poll() is None:
            time.sleep(2)
            if not os.path.exists(sock):
                continue
            if firmware == "seabios":
                txt = vga_text(sock)
            else:
                with open(serial, "rb") as f:
                    txt = f.read().decode(errors="replace")
            if "Hello World" in txt and "Press Enter to reboot" in txt:
                res["greeting"], res["text"] = True, " ".join(txt.split())[-200:]
                break
        if res["greeting"]:
            time.sleep(3)
            res["waited_for_enter"] = p.poll() is None   # must NOT reboot before Enter
            _hmp(sock, "sendkey ret")
            try:
                p.wait(timeout=30)
                res["rebooted"] = True       # QEMU exited because the guest reset (-no-reboot)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    return res
