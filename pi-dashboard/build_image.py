"""
Build PiDash .img from an official Raspberry Pi OS Lite image (works on Windows).

  python build_image.py <raspios-lite.img.xz | .img> <output.img> [--inspect]

Only the FAT boot partition is modified (via pyfatfs): the dashboard app,
the settings file and a cloud-init first-boot hook are added to it.
"""
import lzma
import os
import shutil
import struct
import sys
import warnings

warnings.filterwarnings("ignore")
from pyfatfs.PyFatFS import PyFatFS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_EXT = {".sh", ".py", ".service", ".js", ".css", ".html", ".txt", ""}


def decompress(src, dst):
    if src.endswith(".xz"):
        print("Decompressing", src)
        with lzma.open(src) as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 16 << 20)
    else:
        shutil.copyfile(src, dst)


def boot_partition(img):
    with open(img, "rb") as f:
        mbr = f.read(512)
    assert mbr[510:512] == b"\x55\xaa", "no MBR"
    for i in range(4):
        e = mbr[446 + 16 * i: 462 + 16 * i]
        ptype = e[4]
        lba, n = struct.unpack("<II", e[8:16])
        if ptype in (0x0B, 0x0C, 0x0E, 0x06):
            return lba * 512, n * 512
    raise RuntimeError("FAT boot partition not found")


def copy_part(img, off, size, part, to_img=False):
    with open(img, "r+b" if to_img else "rb") as fi:
        if to_img:
            with open(part, "rb") as fp:
                fi.seek(off)
                shutil.copyfileobj(fp, fi, 16 << 20)
        else:
            fi.seek(off)
            with open(part, "wb") as fp:
                left = size
                while left:
                    chunk = fi.read(min(left, 16 << 20))
                    fp.write(chunk)
                    left -= len(chunk)


def read_text(fs, path):
    with fs.openbin(path, "r") as f:
        return f.read().decode("utf-8", "replace")


def write_bytes(fs, path, data):
    if fs.exists(path):
        fs.remove(path)
    with fs.openbin(path, "w") as f:
        f.write(data)


def put_tree(fs, local, remote):
    fs.makedirs(remote, recreate=True)
    for name in sorted(os.listdir(local)):
        lp, rp = os.path.join(local, name), remote + "/" + name
        if name in ("__pycache__", "data") or name.endswith(".pyc"):
            continue
        if os.path.isdir(lp):
            put_tree(fs, lp, rp)
        else:
            with open(lp, "rb") as f:
                data = f.read()
            if os.path.splitext(name)[1] in TEXT_EXT:
                data = data.replace(b"\r\n", b"\n")
            write_bytes(fs, rp, data)
            print("  +", rp, len(data))


def main():
    src, out = sys.argv[1], sys.argv[2]
    inspect = "--inspect" in sys.argv
    if not os.path.exists(out) or not inspect:
        decompress(src, out)
    off, size = boot_partition(out)
    print("boot partition: offset %d, size %d MB" % (off, size >> 20))
    part = out + ".bootpart"
    copy_part(out, off, size, part)
    fs = PyFatFS(part, encoding="utf-8")
    try:
        if inspect:
            print("root:", sorted(fs.listdir("/")))
            for n in ("cmdline.txt", "user-data", "meta-data", "network-config", "config.txt"):
                if fs.exists("/" + n):
                    print("=====", n)
                    print(read_text(fs, "/" + n))
            return
        boot = os.path.join(HERE, "boot")
        put_tree(fs, os.path.join(HERE, "app"), "/pidash/app")
        put_tree(fs, os.path.join(boot, "pidash"), "/pidash")
        put_tree(fs, os.path.join(boot, "photos"), "/photos")
        put_tree(fs, os.path.join(boot, "videos"), "/videos")
        # startup text goes to a hidden screen, so that only the splash shows
        line = read_text(fs, "/cmdline.txt").strip()
        assert "console=tty1" in line and "root=" in line
        write_bytes(fs, "/cmdline.txt", (line.replace("console=tty1", "console=tty3") + " consoleblank=0 logo.nologo vt.global_cursor_default=0"
                                         " quiet loglevel=3 systemd.show_status=false udev.log_level=3\n").encode())
        write_bytes(fs, "/config.txt", (read_text(fs, "/config.txt").rstrip() + "\n\n[all]\n# PiDash: no rainbow picture at power on\ndisable_splash=1\n").encode())
        with open(os.path.join(boot, "dashboard-settings.txt"), "rb") as f:
            write_bytes(fs, "/dashboard-settings.txt", f.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        with open(os.path.join(HERE, "README.txt"), "rb") as f:
            write_bytes(fs, "/PIDASH-README.txt", f.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        # cloud-init first-boot hook (Raspberry Pi OS Trixie uses cloud-init)
        with open(os.path.join(boot, "user-data"), "rb") as f:
            write_bytes(fs, "/user-data", f.read().replace(b"\r\n", b"\n"))
        print("root now:", sorted(fs.listdir("/")))
    finally:
        fs.close()
    if not inspect:
        copy_part(out, off, size, part, to_img=True)
        print("Wrote", out)
    os.remove(part)


if __name__ == "__main__":
    main()
