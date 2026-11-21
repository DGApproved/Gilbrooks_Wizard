"""Hello World boot example: stock baseline, preserve/fresh choice, build state, target-only writes.
Image files and mocks only - no real device is ever opened."""
import json
import os
import shutil
import subprocess

import pytest

from usbprep import bootcontent as B, constants as C, workflow as w
from usbprep.appcopy import DirBackend, MtoolsBackend, install_app
from usbprep.layout import read_image_layout
from usbprep.runner import MockRunner

from helpers import blank_lsblk_device, session_with
import qemu_boot

HAVE_IMG_TOOLS = all(shutil.which(t) for t in ("sgdisk", "mcopy", "mkswap"))
needs_img = pytest.mark.skipif(not HAVE_IMG_TOOLS, reason="sgdisk/mtools/mkswap missing")
BIOS_BIN = os.path.join(B.stock_dir("BIOS"), "hello_bios.bin")
EFI_BIN = os.path.join(B.stock_dir("EFI"), "BOOTX64.EFI")


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def make_source_ai(tmp, stock=True):
    """A fake running-USB AI folder holding a copy of the stock boot example."""
    ai = os.path.join(tmp, "srcusb", "AI")
    os.makedirs(ai)
    if stock:
        shutil.copytree(os.path.join(B.STOCK_ROOT, "scr"), os.path.join(ai, "scr"))
        shutil.copytree(os.path.join(B.STOCK_ROOT, "src"), os.path.join(ai, "src"))
    return ai


def edit_asm(ai, text=b"Hi Darek"):
    p = os.path.join(ai, "scr", "BIOS", "hello.asm")
    s = _read(p).replace(b'db "Hello World"', b'db "' + text + b'"')
    with open(p, "wb") as f:
        f.write(s)


@pytest.fixture(scope="module")
def blank_image(tmp_path_factory):
    d = tmp_path_factory.mktemp("bootimg")
    p = str(d / "base1234.img")
    w.create_image(p, 5600 * C.MiB)
    return p


def fresh_copy(blank, tmp, name="t1234.img"):
    dst = os.path.join(tmp, name)
    subprocess.run(["cp", "--sparse=always", blank, dst], check=True)
    return dst


def fat_tree(img, rel, local):
    p1 = sorted(read_image_layout(img).partitions, key=lambda p: p.number)[0]
    be = MtoolsBackend(img, p1.start)
    if not be.exists(rel):
        return None
    be.get_tree(rel, local)
    return B.tree_hashes(local)


def fat_file_sha(img, rel):
    p1 = sorted(read_image_layout(img).partitions, key=lambda p: p.number)[0]
    be = MtoolsBackend(img, p1.start)
    return be.file_sha256(rel) if be.exists_file(rel) else None


# ---------------- stock baseline ----------------
def test_stock_manifest_integrity_and_tamper(tmp_path):
    assert B.verify_stock() == []
    m = B.load_stock_manifest()["files"]
    assert {"scr/BIOS/hello.asm", "scr/BIOS/build.sh", "scr/BIOS/hello_bios.bin", "src/EFI/hello.c",
            "src/EFI/build.sh", "src/EFI/BOOTX64.EFI"} <= set(m)
    cp = str(tmp_path / "stock")
    shutil.copytree(B.STOCK_ROOT, cp)
    with open(os.path.join(cp, "scr", "BIOS", "hello.asm"), "ab") as f:
        f.write(b"; x\n")
    open(os.path.join(cp, "src", "EFI", "extra.txt"), "w").close()
    bad = B.verify_stock(cp)
    assert "modified scr/BIOS/hello.asm" in bad and "unexpected src/EFI/extra.txt" in bad
    with pytest.raises(B.BootError, match="integrity"):
        B.stage(str(tmp_path / "w"), None, stock_root=cp)


def test_stock_artifacts_valid_and_build_state():
    B.validate_artifact("BIOS", BIOS_BIN)
    B.validate_artifact("EFI", EFI_BIN)
    assert len(_read(BIOS_BIN)) <= 440
    for comp in ("BIOS", "EFI"):
        assert B.build_state(B.stock_dir(comp), comp)["state"] == "valid"


@pytest.mark.skipif(not (shutil.which("nasm") and shutil.which("x86_64-w64-mingw32-gcc")), reason="nasm/mingw missing")
def test_stock_artifacts_reproducible_with_approved_build(tmp_path):
    for comp, art in (("BIOS", BIOS_BIN), ("EFI", EFI_BIN)):
        out = B.approved_build(B.stock_dir(comp), comp, str(tmp_path / comp))
        assert B.sha256_file(out) == B.sha256_file(art), f"{comp} stock artifact is not reproducible"


@pytest.mark.skipif(not shutil.which("nasm"), reason="nasm missing")
def test_stock_build_script_records_matching_build_info(tmp_path):
    d = str(tmp_path / "BIOS")
    shutil.copytree(B.stock_dir("BIOS"), d)
    os.remove(os.path.join(d, "hello_bios.bin"))
    subprocess.run(["sh", os.path.join(d, "build.sh")], check=True, capture_output=True)
    assert B.build_state(d, "BIOS")["state"] == "valid"
    assert _read(os.path.join(d, "hello_bios.bin")) == _read(BIOS_BIN)


def test_invalid_artifacts_rejected(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x90" * 441)
    with pytest.raises(B.BootError):
        B.validate_artifact("BIOS", str(big))
    with pytest.raises(B.BootError, match="PE32"):
        B.validate_artifact("EFI", BIOS_BIN)


# ---------------- detection ----------------
def test_detect_stock_modified_alias_missing(tmp_path):
    ai = make_source_ai(str(tmp_path))
    rep = B.detect(ai)
    assert not B.is_modified(rep) and rep["BIOS"]["status"] == "stock"
    edit_asm(ai)
    rep = B.detect(ai)
    assert rep["BIOS"]["status"] == "modified" and "scr/BIOS/hello.asm (changed)" in rep["BIOS"]["changes"]
    assert rep["EFI"]["status"] == "stock"
    assert B.detect(None)["BIOS"]["status"] == "missing"
    ai2 = make_source_ai(str(tmp_path / "b"), stock=False)
    os.makedirs(os.path.join(ai2, "src", "BIOS"))
    open(os.path.join(ai2, "src", "BIOS", "mine.asm"), "w").close()
    rep = B.detect(ai2)
    assert rep["BIOS"]["dirs"] == ["src/BIOS"] and rep["BIOS"]["status"] == "modified"


def test_build_state_stale_unbuilt_tampered(tmp_path):
    ai = make_source_ai(str(tmp_path))
    d = os.path.join(ai, "scr", "BIOS")
    edit_asm(ai)
    assert B.build_state(d, "BIOS")["state"] == "stale"
    os.remove(os.path.join(d, "hello_bios.bin"))
    assert B.build_state(d, "BIOS")["state"] == "unbuilt"
    e = os.path.join(ai, "src", "EFI")
    with open(os.path.join(e, "BOOTX64.EFI"), "ab") as f:
        f.write(b"x")
    st = B.build_state(e, "EFI")
    assert st["state"] == "stale" and "differs" in st["reason"]
    os.remove(os.path.join(e, B.BUILD_INFO))
    assert B.build_state(e, "EFI")["state"] == "stale"


# ---------------- staging decisions (host temp only) ----------------
def test_stage_unmodified_is_stock(tmp_path):
    for src in (None, make_source_ai(str(tmp_path))):
        work = str(tmp_path / f"w{bool(src)}")
        os.makedirs(work)
        c = B.stage(work, src)
        assert c.mode == "fresh"
        assert B.tree_hashes(c.tree) == {k: v for k, v in B.load_stock_manifest()["files"].items()}
        assert _read(c.install["BIOS"]) == _read(BIOS_BIN)


def test_stage_modified_requires_choice(tmp_path):
    ai = make_source_ai(str(tmp_path))
    edit_asm(ai)
    work = str(tmp_path / "w")
    os.makedirs(work)
    with pytest.raises(B.BootChoiceRequired, match="preserve.*fresh"):
        B.stage(work, ai)
    with pytest.raises(B.BootError):
        B.stage(str(tmp_path / "w2"), ai, choice="keep")


def test_stage_preserve_stale_never_substitutes_stock(tmp_path):
    ai = make_source_ai(str(tmp_path))
    edit_asm(ai)
    before = B.tree_hashes(ai)
    work = str(tmp_path / "w")
    os.makedirs(work)
    c = B.stage(work, ai, "preserve")
    assert B.tree_hashes(ai) == before                      # source untouched
    assert c.install["BIOS"] is None                         # stale -> no BIOS boot code
    assert c.install["EFI"] and _read(c.install["EFI"]) == _read(EFI_BIN)   # unmodified EFI still valid
    bios = os.path.join(c.tree, "scr", "BIOS")
    assert b"Hi Darek" in _read(os.path.join(bios, "hello.asm"))
    assert not os.path.exists(os.path.join(bios, "hello_bios.bin"))
    assert os.path.exists(os.path.join(bios, "hello_bios.bin.stale"))
    assert any("STALE" in l and "never substituted" in l for l in c.log)


@pytest.mark.skipif(not shutil.which("nasm"), reason="nasm missing")
def test_stage_preserve_stale_with_build_consent(tmp_path):
    ai = make_source_ai(str(tmp_path))
    edit_asm(ai)
    work = str(tmp_path / "w")
    os.makedirs(work)
    c = B.stage(work, ai, "preserve", build_consent=True)
    code = _read(c.install["BIOS"])
    assert b"Hi Darek" in code and code != _read(BIOS_BIN)
    assert B.build_state(os.path.dirname(c.install["BIOS"]), "BIOS")["state"] == "valid"
    assert not os.path.exists(os.path.join(ai, "scr", "BIOS", "hello_bios.bin.stale"))   # source untouched


@pytest.mark.skipif(not shutil.which("nasm"), reason="nasm missing")
def test_stage_preserve_valid_user_build(tmp_path):
    ai = make_source_ai(str(tmp_path))
    edit_asm(ai)
    subprocess.run(["sh", os.path.join(ai, "scr", "BIOS", "build.sh")], check=True, capture_output=True)
    work = str(tmp_path / "w")
    os.makedirs(work)
    c = B.stage(work, ai, "preserve")
    assert b"Hi Darek" in _read(c.install["BIOS"])


def test_stage_fresh_ignores_user_changes(tmp_path):
    ai = make_source_ai(str(tmp_path))
    edit_asm(ai)
    open(os.path.join(ai, "src", "EFI", "notes.txt"), "w").close()
    before = B.tree_hashes(ai)
    work = str(tmp_path / "w")
    os.makedirs(work)
    c = B.stage(work, ai, "fresh")
    assert B.tree_hashes(c.tree) == B.load_stock_manifest()["files"]
    assert B.tree_hashes(ai) == before


# ---------------- MBR boot-code patch on an IMAGE ----------------
@needs_img
def test_bios_patch_preserves_table_signature_and_gpt(blank_image, tmp_path):
    img = fresh_copy(blank_image, str(tmp_path))
    assert w.prepare_image(img, True, "ERASE 1234").ok
    before = _read(img)[:34 * 512]
    log = B.patch_mbr_image(img, BIOS_BIN, str(tmp_path / "bk"), B.stock_codes())
    after = _read(img)[:34 * 512]
    code = _read(BIOS_BIN)
    assert after[:len(code)] == code and after[len(code):440] == b"\0" * (440 - len(code))
    assert after[440:] == before[440:]                      # disk signature, entries, 0x55AA, GPT header+entries
    assert _read(str(tmp_path / "bk" / "mbr-sector0.bak")) == before[:512]
    assert "verified unchanged" in log[0]
    v = subprocess.run(["sgdisk", "-v", img], capture_output=True, text=True)
    assert "No problems found" in v.stdout
    # same stock code again is fine (idempotent); unknown code refused without consent
    B.patch_mbr_image(img, BIOS_BIN, str(tmp_path / "bk"), B.stock_codes())
    other = tmp_path / "other.bin"
    other.write_bytes(b"\xEB\xFE")
    B.patch_mbr_image(img, str(other), str(tmp_path / "bk"), B.stock_codes())     # replacing OUR known code: ok
    with pytest.raises(B.BootError, match="unknown boot code"):                    # foreign code now on disk
        B.patch_mbr_image(img, BIOS_BIN, str(tmp_path / "bk"), B.stock_codes())
    B.patch_mbr_image(img, BIOS_BIN, str(tmp_path / "bk"), B.stock_codes(), overwrite_unknown=True)
    assert _read(img)[440:34 * 512] == before[440:]


def test_bios_patch_refuses_non_gpt_and_non_files(tmp_path):
    raw = tmp_path / "raw.img"
    raw.write_bytes(b"\0" * 4096)
    with pytest.raises(B.BootError, match="0x55AA"):
        B.patch_mbr_image(str(raw), BIOS_BIN, str(tmp_path / "bk"))
    mbr = bytearray(4096)
    mbr[510:512] = b"\x55\xAA"
    mbr[450] = 0x83
    raw.write_bytes(bytes(mbr))
    with pytest.raises(B.BootError, match="protective"):
        B.patch_mbr_image(str(raw), BIOS_BIN, str(tmp_path / "bk"))
    with pytest.raises(B.BootError, match="regular image"):
        B.patch_mbr_image("/dev/null", BIOS_BIN, str(tmp_path / "bk"))
    with pytest.raises((B.BootError, OSError)):
        B.patch_mbr_device(str(raw), BIOS_BIN, str(tmp_path / "bk"))


# ---------------- end-to-end fresh preparation of IMAGES ----------------
@needs_img
def test_fresh_unmodified_deploys_stock(blank_image, tmp_path):
    img = fresh_copy(blank_image, str(tmp_path))
    src = make_source_ai(str(tmp_path))
    r = w.fresh_install_image(img, True, "ERASE 1234", None, src)
    assert r.ok, r.log
    stock = B.load_stock_manifest()["files"]
    assert fat_tree(img, "AI/scr", str(tmp_path / "s")) == {k[4:]: v for k, v in stock.items() if k.startswith("scr/")}
    assert fat_tree(img, "AI/src", str(tmp_path / "e")) == {k[4:]: v for k, v in stock.items() if k.startswith("src/")}
    assert fat_file_sha(img, B.EFI_FALLBACK) == B.sha256_file(EFI_BIN)
    assert _read(img)[:len(_read(BIOS_BIN))] == _read(BIOS_BIN)
    globals()["_STOCK_IMG"] = img


@needs_img
def test_fresh_modified_without_choice_writes_nothing(blank_image, tmp_path, monkeypatch):
    img = fresh_copy(blank_image, str(tmp_path))
    src = make_source_ai(str(tmp_path))
    edit_asm(src)
    head = _read(img)[:1 << 20]
    called = []
    monkeypatch.setattr(w, "prepare_image", lambda *a, **k: called.append(a))
    with pytest.raises(B.BootChoiceRequired):
        w.fresh_install_image(img, True, "ERASE 1234", None, src)
    assert called == [] and _read(img)[:1 << 20] == head


@needs_img
def test_fresh_modified_preserve_and_fresh(blank_image, tmp_path):
    src = make_source_ai(str(tmp_path))
    edit_asm(src)
    open(os.path.join(src, "src", "EFI", "notes.txt"), "w").write("mine")
    before = B.tree_hashes(os.path.dirname(src))
    # preserve (BIOS stale, not rebuilt) -> user sources copied, no BIOS code, EFI stock-valid installed
    img = fresh_copy(blank_image, str(tmp_path), "p1234.img")
    r = w.fresh_install_image(img, True, "ERASE 1234", "preserve", src)
    assert r.ok
    t = fat_tree(img, "AI/scr", str(tmp_path / "p"))
    assert "BIOS/hello_bios.bin" not in t and "BIOS/hello_bios.bin.stale" in t
    assert b"Hi Darek" in _read(str(tmp_path / "p" / "BIOS" / "hello.asm"))
    assert "EFI/notes.txt" in fat_tree(img, "AI/src", str(tmp_path / "pe"))
    assert _read(img)[:440] == b"\0" * 440                   # stock NOT substituted for edited code
    assert any("NOT installed" in l for l in r.log)
    # fresh -> stock only
    img2 = fresh_copy(blank_image, str(tmp_path), "f1234.img")
    assert w.fresh_install_image(img2, True, "ERASE 1234", "fresh", src).ok
    t2 = fat_tree(img2, "AI/src", str(tmp_path / "f"))
    assert "EFI/notes.txt" not in t2
    assert _read(img2)[:len(_read(BIOS_BIN))] == _read(BIOS_BIN)
    assert B.tree_hashes(os.path.dirname(src)) == before      # source USB tree untouched in both cases


@needs_img
def test_prepared_image_keeps_custom_boot_content(blank_image, tmp_path):
    img = fresh_copy(blank_image, str(tmp_path))
    assert w.fresh_install_image(img, True, "ERASE 1234").ok
    mbr = _read(img)[:512]
    r = w.fresh_install_image(img, True, "ERASE 1234", "fresh")
    assert "preserved" in " ".join(r.log) and _read(img)[:512] == mbr


# ---------------- device flow (mocks only) ----------------
def test_duplicate_modified_no_choice_no_disk_writes(tmp_path):
    s, r = session_with([blank_lsblk_device("sdy", serial="OTHER0001")])
    s.source_ai = make_source_ai(str(tmp_path))
    edit_asm(s.source_ai)
    before = B.tree_hashes(s.source_ai)
    with pytest.raises(B.BootChoiceRequired):
        s.duplicate_to(s.select("/dev/sdy"), execute=True, confirmation="ERASE 0001", mounted_sources=set())
    assert r.calls == [] or all(c[0] == "lsblk" for c in r.calls)
    assert B.tree_hashes(s.source_ai) == before


def test_install_boot_targets_only_selected_usb(tmp_path):
    s, r = session_with([blank_lsblk_device("sdy", serial="OTHER0001")],
                        extra={"sudo": (0, "BIOS boot code written", "")})
    dev = s.select("/dev/sdy")
    esp = tmp_path / "esp"
    (esp / "AI").mkdir(parents=True)
    work = str(tmp_path / "w")
    os.makedirs(work)
    log = s._install_boot(dev, DirBackend(str(esp)), B.stage(work, None), "sudo")
    assert B.sha256_file(str(esp / "EFI" / "BOOT" / "BOOTX64.EFI")) == B.sha256_file(EFI_BIN)
    patch = [c for c in r.calls if c[0] == "sudo"]
    assert len(patch) == 1 and "/dev/sdy" in patch[0] and "patch-mbr" in patch[0]
    flat = " ".join(" ".join(map(str, c)) for c in r.calls)
    for tool in B.FORBIDDEN_TOOLS:
        assert tool not in flat
    assert any("verified" in l for l in log)
    # device disappeared -> refused before any further write
    s2, r2 = session_with([], extra={"sudo": (0, "", "")})
    os.makedirs(str(tmp_path / "w2"))
    content = B.stage(str(tmp_path / "w2"), None)
    with pytest.raises(Exception, match="changed or disappeared"):
        s2._install_boot(dev, DirBackend(str(tmp_path / "x")), content, "sudo")
    assert not [c for c in r2.calls if c[0] == "sudo"]


def test_guard_and_no_host_boot_tools_in_code():
    for t in ("efibootmgr", "bcdedit", "/usr/sbin/grub-install", "bless"):
        with pytest.raises(B.BootError, match="refusing host boot tool"):
            B.guard_command([t, "--create"])
    B.guard_command(["sudo", "env"])
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "usbprep")
    for fn in os.listdir(root):
        if fn.endswith(".py") and fn != "bootcontent.py":
            src = open(os.path.join(root, fn)).read()
            for tool in ("efibootmgr", "bcdedit", "grub-install", "update-grub"):
                assert f'"{tool}"' not in src, f"{fn} invokes {tool}"


def test_app_update_carries_custom_boot_folders(tmp_path):
    esp = tmp_path / "esp"
    esp.mkdir()
    install_app(DirBackend(str(esp)))
    (esp / "AI" / "scr" / "BIOS").mkdir(parents=True)
    (esp / "AI" / "scr" / "BIOS" / "hello.asm").write_text("; mine")
    mf = esp / "AI" / C.MANIFEST_NAME
    m = json.loads(mf.read_text())
    m["files"]["old-file.txt"] = "0" * 64
    mf.write_text(json.dumps(m))
    install_app(DirBackend(str(esp)), allow_update=True)
    assert (esp / "AI" / "scr" / "BIOS" / "hello.asm").read_text() == "; mine"


def test_cli_stops_before_write_when_choice_missing(tmp_path):
    src = make_source_ai(str(tmp_path))
    edit_asm(src)
    img = str(tmp_path / "c1234.img")
    with open(img, "wb") as f:
        f.truncate(64 * C.MiB)
    p = subprocess.run(["python3", "-m", "usbprep", "image", "prepare", img, "--execute", "--confirm", "ERASE 1234",
                        "--source-ai", src], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       cwd=os.path.dirname(os.path.dirname(__file__)))
    assert p.returncode == 3 and "STOPPED before any write" in p.stderr
    assert _read(img) == b"\0" * (64 * C.MiB)
    st = json.loads(subprocess.run(["python3", "-m", "usbprep", "boot-status", "--ai", src], capture_output=True,
                                   text=True, cwd=os.path.dirname(os.path.dirname(__file__))).stdout)
    assert st["modified"] is True and st["components"]["BIOS"]["build"]["state"] == "stale"


# ---------------- emulator boot (image only, snapshot=on) ----------------
@needs_img
@pytest.mark.skipif(not qemu_boot.QEMU, reason="qemu-system-x86_64 not installed")
@pytest.mark.parametrize("fw", ["seabios", "ovmf"])
def test_qemu_boots_stock_example(blank_image, tmp_path, fw):
    if fw == "ovmf" and not qemu_boot.OVMF:
        pytest.skip("OVMF firmware not installed")
    img = fresh_copy(blank_image, str(tmp_path), f"q{fw[:2]}1234.img")
    assert w.fresh_install_image(img, True, "ERASE 1234").ok
    res = qemu_boot.boot(img, fw, str(tmp_path), 150)
    print(fw, res)
    assert res["greeting"], res
    assert res.get("waited_for_enter"), "rebooted before Enter was pressed"
    assert res["rebooted"], res
