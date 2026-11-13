#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
WinUSB-SteamOS worker: turns a Windows ISO into a bootable UEFI USB stick.

Runs as root (started by the GUI through pkexec, or by hand with sudo).
Designed for immutable systems such as SteamOS:
  * never writes outside /tmp (no /media, no /mnt, nothing in the root fs)
  * partition table is written directly in Python (no sfdisk/parted needed)
  * mkfs.fat and wimlib-imagex are taken from the AppImage bundle

Layout produced (same approach as Rufus "FAT32 + split WIM" / WoeUSB-ng):
  MBR, one primary FAT32 (LBA, type 0x0C) partition, active flag set,
  all ISO files copied, install.wim > 4 GiB split into install.swm parts.

Progress is reported as JSON lines on stdout:
  {"t": "log", "msg": "..."}
  {"t": "progress", "pct": 12.3, "stage": "..."}
  {"t": "done", "msg": "..."}
  {"t": "error", "msg": "..."}
"""

import argparse
import fcntl
import json
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

VERSION = "1.0.0"

FAT32_MAX_FILE = 4 * 1024 ** 3 - 1          # 4 GiB - 1 byte
SPLIT_SIZE_MB = 3800                         # size of each install.swm part
COPY_CHUNK = 4 * 1024 * 1024
SYNC_EVERY = 32 * 1024 * 1024                # fdatasync interval -> honest progress
ALIGN = 1024 * 1024                          # partition starts at 1 MiB
FS_LABEL = "WIN_INSTALL"

BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268
BLKRRPART = 0x125F
BLKFLSBUF = 0x1261

CRITICAL_MOUNTS = {"/", "/home", "/var", "/usr", "/boot", "/efi", "/etc",
                   "/opt", "/srv", "/root", "/var/home", "/run/media/root"}

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

JSON_MODE = True
_last_progress = 0.0


def emit(kind, **kw):
    kw["t"] = kind
    if JSON_MODE:
        sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        if kind == "progress":
            sys.stdout.write("\r[%5.1f%%] %-50s" % (kw["pct"], kw.get("stage", "")[:50]))
            sys.stdout.flush()
        elif kind == "error":
            sys.stdout.write("\nFEHLER: %s\n" % kw["msg"])
        else:
            sys.stdout.write("\n" + kw.get("msg", ""))
        sys.stdout.flush()


def log(msg):
    emit("log", msg=msg)


def progress(pct, stage, force=False):
    global _last_progress
    now = time.monotonic()
    if force or now - _last_progress > 0.2:
        _last_progress = now
        emit("progress", pct=round(max(0.0, min(100.0, pct)), 1), stage=stage)


class Cancelled(Exception):
    pass


class WorkerError(Exception):
    pass


CANCEL = threading.Event()


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def watch_stdin():
    """GUI mode: closing our stdin (or writing 'cancel') aborts the job."""
    def run():
        try:
            for line in sys.stdin:
                if line.strip().lower() == "cancel":
                    CANCEL.set()
                    return
        except Exception:
            pass
        CANCEL.set()          # EOF -> GUI went away
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def appdir():
    env = os.environ.get("WINUSB_APPDIR")
    if env and Path(env).is_dir():
        return Path(env)
    # src/winusb_worker.py  or  AppDir/usr/share/winusb/winusb_worker.py
    here = Path(__file__).resolve()
    if len(here.parents) >= 4 and (here.parents[3] / "AppRun").exists():
        return here.parents[3]
    return None


def find_tool(name):
    ad = appdir()
    if ad:
        p = ad / "usr" / "bin" / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/local/sbin"):
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    p = shutil.which(name)
    if p:
        return p
    return None


def need_tool(name):
    p = find_tool(name)
    if not p:
        raise WorkerError("Benötigtes Programm '%s' wurde nicht gefunden." % name)
    return p


def run(cmd, check=True, quiet=False):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")
    if not quiet and proc.stdout.strip():
        for line in proc.stdout.strip().splitlines()[-15:]:
            log("  " + line)
    if check and proc.returncode != 0:
        raise WorkerError("Befehl fehlgeschlagen (%d): %s\n%s" %
                          (proc.returncode, " ".join(cmd), proc.stdout.strip()[-800:]))
    return proc


# --------------------------------------------------------------------------
# block device helpers
# --------------------------------------------------------------------------

def sys_name(dev):
    st = os.stat(dev)
    maj, mnr = os.major(st.st_rdev), os.minor(st.st_rdev)
    link = Path("/sys/dev/block/%d:%d" % (maj, mnr))
    return Path(os.path.realpath(link))


def partition_names(disk_sys):
    disk = disk_sys.name
    out = []
    for child in sorted(disk_sys.iterdir()):
        if child.name.startswith(disk) and (child / "partition").exists():
            out.append(child.name)
    return out


def partition_dev(disk_dev, n=1):
    base = os.path.basename(disk_dev)
    sep = "p" if base[-1].isdigit() else ""
    return "/dev/%s%s%d" % (base, sep, n)


def read_mounts():
    res = []
    with open("/proc/self/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            src = parts[0].replace("\\040", " ")
            mnt = parts[1].replace("\\040", " ")
            res.append((src, mnt, parts[2]))
    return res


def mounts_of_disk(disk_sys):
    names = {disk_sys.name} | set(partition_names(disk_sys))
    found = []
    for src, mnt, _ in read_mounts():
        if not src.startswith("/dev/"):
            continue
        try:
            real = os.path.realpath(src)
            if os.path.basename(real) in names:
                found.append((real, mnt))
        except OSError:
            pass
    return found


def holders_busy(disk_sys):
    """dm/LVM/RAID on top of the disk?"""
    for name in [disk_sys.name] + partition_names(disk_sys):
        h = disk_sys / name / "holders" if name != disk_sys.name else disk_sys / "holders"
        if h.exists() and any(h.iterdir()):
            return True
    return False


def sysfs_flag(path):
    try:
        return Path(path).read_text().strip() == "1"
    except OSError:
        return False


def is_removable(disk_sys):
    if sysfs_flag(disk_sys / "removable"):
        return True
    # USB attached disks (removable=0 for many USB-SSD/sticks)
    real = os.path.realpath(disk_sys / "device") if (disk_sys / "device").exists() else ""
    if "/usb" in real:
        return True
    try:
        out = subprocess.run(["lsblk", "-dno", "HOTPLUG,TRAN", "/dev/" + disk_sys.name],
                             stdout=subprocess.PIPE, text=True).stdout.split()
        if out and (out[0] == "1" or (len(out) > 1 and out[1] == "usb")):
            return True
    except Exception:
        pass
    return False


def device_size(fd):
    buf = fcntl.ioctl(fd, BLKGETSIZE64, b"\0" * 8)
    return struct.unpack("Q", buf)[0]


def sector_size(fd):
    try:
        buf = fcntl.ioctl(fd, BLKSSZGET, b"\0" * 4)
        return struct.unpack("i", buf)[0] or 512
    except OSError:
        return 512


def validate_target(dev, force_any):
    if not os.path.exists(dev):
        raise WorkerError("Gerät %s existiert nicht." % dev)
    st = os.stat(dev)
    if not stat.S_ISBLK(st.st_mode):
        raise WorkerError("%s ist kein Blockgerät." % dev)
    dsys = sys_name(dev)
    if (dsys / "partition").exists():
        raise WorkerError("%s ist eine Partition. Bitte das ganze Gerät wählen (z.B. /dev/sdc)." % dev)
    for real, mnt in mounts_of_disk(dsys):
        if mnt in CRITICAL_MOUNTS or mnt.startswith("/usr") or mnt.startswith("/home") \
                or mnt.startswith("/var") or mnt.startswith("/boot"):
            raise WorkerError("%s enthält das System (%s ist unter %s eingehängt). Abbruch!" %
                              (dev, real, mnt))
    if holders_busy(dsys):
        raise WorkerError("%s wird von LVM/RAID/Verschlüsselung benutzt. Abbruch!" % dev)
    if not force_any and not is_removable(dsys):
        raise WorkerError("%s ist kein USB-/Wechseldatenträger. Aus Sicherheitsgründen abgebrochen." % dev)
    return dsys


# --------------------------------------------------------------------------
# mounting (only below /tmp!)
# --------------------------------------------------------------------------

def umount(path, lazy_ok=True):
    tool = find_tool("umount") or "umount"
    for _ in range(5):
        p = subprocess.run([tool, path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode == 0:
            return True
        time.sleep(0.6)
    if lazy_ok:
        subprocess.run([tool, "-l", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def is_mounted(path):
    path = os.path.realpath(path)
    return any(os.path.realpath(m) == path for _, m, _ in read_mounts())


def mount_iso(iso, target):
    mount = need_tool("mount")
    errors = []
    for fstype in os.environ.get("WINUSB_ISO_FSTYPES", "udf,iso9660").split(","):
        p = subprocess.run([mount, "-o", "loop,ro", "-t", fstype, iso, target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode == 0:
            if find_ci(Path(target), "sources") is not None:
                log("ISO eingehängt (%s)." % fstype)
                return
            umount(target)
            errors.append("%s: kein 'sources'-Ordner" % fstype)
        else:
            errors.append("%s: %s" % (fstype, p.stdout.strip()))
    raise WorkerError("ISO konnte nicht geöffnet werden:\n" + "\n".join(errors))


def find_ci(base, *parts):
    """case-insensitive path lookup"""
    cur = base
    for part in parts:
        if not cur.is_dir():
            return None
        match = None
        for e in cur.iterdir():
            if e.name.lower() == part.lower():
                match = e
                break
        if match is None:
            return None
        cur = match
    return cur


# --------------------------------------------------------------------------
# partitioning + formatting
# --------------------------------------------------------------------------

def unmount_all(dsys):
    for real, mnt in sorted(mounts_of_disk(dsys), key=lambda x: -len(x[1])):
        log("Hänge %s aus (%s) ..." % (real, mnt))
        if not umount(mnt):
            log("  Warnung: %s konnte nicht normal ausgehängt werden." % mnt)
    # swap on that disk?
    try:
        with open("/proc/swaps") as f:
            for line in f.readlines()[1:]:
                src = line.split()[0]
                if os.path.basename(os.path.realpath(src)) in ([dsys.name] + partition_names(dsys)):
                    run([need_tool("swapoff"), src], check=False)
    except OSError:
        pass


def write_mbr(dev):
    """Wipe old signatures and write an MBR with one FAT32 LBA partition."""
    fd = os.open(dev, os.O_RDWR | os.O_EXCL | os.O_SYNC)
    try:
        size = device_size(fd)
        ss = sector_size(fd)
        if size < 1024 ** 3:
            raise WorkerError("Gerät ist zu klein (%d MB)." % (size // 1024 ** 2))
        zero = b"\0" * ALIGN
        # beginning (MBR, GPT header, old FS superblocks) ...
        os.pwrite(fd, zero, 0)
        os.pwrite(fd, zero, ALIGN)
        # ... and end (backup GPT)
        os.pwrite(fd, zero, size - ALIGN)

        total_sectors = size // ss
        start = ALIGN // ss
        count = total_sectors - start
        count -= count % (ALIGN // ss)               # end on a 1 MiB boundary
        count = min(count, 0xFFFFFFFF - start)

        mbr = bytearray(512)
        mbr[440:444] = uuid.uuid4().bytes[:4]        # disk signature
        entry = struct.pack("<B3sB3sII", 0x80, b"\xfe\xff\xff", 0x0C, b"\xfe\xff\xff",
                            start, count)
        mbr[446:462] = entry
        mbr[510] = 0x55
        mbr[511] = 0xAA
        os.pwrite(fd, bytes(mbr), 0)
        os.fsync(fd)
        try:
            fcntl.ioctl(fd, BLKFLSBUF)
        except OSError:
            pass
        return size, ss, start, count
    finally:
        os.close(fd)


def reread_partitions(dev, dsys):
    ok = False
    for _ in range(10):
        try:
            fd = os.open(dev, os.O_RDONLY)
            try:
                fcntl.ioctl(fd, BLKRRPART)
                ok = True
            finally:
                os.close(fd)
            break
        except OSError:
            time.sleep(0.5)
    if not ok:
        for cmd in (["partx", "-u", dev], ["blockdev", "--rereadpt", dev]):
            tool = find_tool(cmd[0])
            if tool and subprocess.run([tool] + cmd[1:], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL).returncode == 0:
                ok = True
                break
    udevadm = find_tool("udevadm")
    if udevadm:
        subprocess.run([udevadm, "settle", "--timeout=10"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    part = partition_dev(dev, 1)
    pname = os.path.basename(part)
    for _ in range(40):
        if os.path.exists(part) and stat.S_ISBLK(os.stat(part).st_mode):
            return part
        sysdev = dsys / pname / "dev"
        if sysdev.exists():
            # no udev (container / minimal system) -> create node ourselves
            try:
                maj, mnr = map(int, sysdev.read_text().strip().split(":"))
                os.mknod(part, 0o660 | stat.S_IFBLK, os.makedev(maj, mnr))
                return part
            except FileExistsError:
                return part
            except OSError:
                pass
        time.sleep(0.25)
    raise WorkerError("Die neue Partition %s ist nicht erschienen. Stick abziehen, neu einstecken "
                      "und nochmal versuchen." % part)


def format_fat32(part):
    mkfs = need_tool("mkfs.fat")
    # clear the first MiB of the partition (old superblocks)
    fd = os.open(part, os.O_RDWR | os.O_SYNC)
    try:
        os.pwrite(fd, b"\0" * ALIGN, 0)
    finally:
        os.close(fd)
    run([mkfs, "-F", "32", "-n", FS_LABEL, part], quiet=True)


# --------------------------------------------------------------------------
# copying
# --------------------------------------------------------------------------

class Progress:
    def __init__(self, total):
        self.total = max(total, 1)
        self.done = 0
        self.lo, self.hi = 5.0, 97.0

    def add(self, n, stage):
        self.done += n
        self.show(stage)

    def show(self, stage, extra=0):
        frac = min(1.0, (self.done + extra) / self.total)
        progress(self.lo + (self.hi - self.lo) * frac, stage)


def copy_file(src, dst, prog, label):
    since_sync = 0
    with open(src, "rb", buffering=0) as fi, open(dst, "wb", buffering=0) as fo:
        while True:
            check_cancel()
            buf = fi.read(COPY_CHUNK)
            if not buf:
                break
            fo.write(buf)
            since_sync += len(buf)
            if since_sync >= SYNC_EVERY:
                os.fdatasync(fo.fileno())
                since_sync = 0
            prog.add(len(buf), label)
        os.fdatasync(fo.fileno())


def split_wim(wim, dst_dir, prog):
    wimlib = need_tool("wimlib-imagex")
    target = os.path.join(dst_dir, "install.swm")
    log("install.wim ist größer als 4 GB -> wird in install.swm-Teile aufgeteilt ...")
    proc = subprocess.Popen([wimlib, "split", str(wim), target, str(SPLIT_SIZE_MB)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    out_lines = []

    def reader():
        for line in proc.stdout:
            out_lines.append(line.rstrip())
    t = threading.Thread(target=reader, daemon=True)
    t.start()

    def written():
        s = 0
        for e in os.scandir(dst_dir):
            if e.name.lower().startswith("install") and e.name.lower().endswith(".swm"):
                try:
                    s += e.stat().st_size
                except OSError:
                    pass
        return s

    wsize = os.path.getsize(wim)
    last_sync = time.monotonic()
    while proc.poll() is None:
        if CANCEL.is_set():
            proc.terminate()
            proc.wait()
            raise Cancelled()
        if time.monotonic() - last_sync > 3:
            os.sync()                      # keep the page cache from piling up
            last_sync = time.monotonic()
        prog.show("Teile install.wim auf", extra=min(written(), wsize))
        time.sleep(0.4)
    t.join(timeout=2)
    if proc.returncode != 0:
        raise WorkerError("wimlib-imagex split fehlgeschlagen:\n" + "\n".join(out_lines[-10:]))
    prog.add(wsize, "Teile install.wim auf")
    parts = sorted(e.name for e in os.scandir(dst_dir) if e.name.lower().endswith(".swm"))
    log("Aufgeteilt in: " + ", ".join(parts))


AUTOUNATTEND_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<!-- Created by WinUSB-SteamOS -->
<unattend xmlns="urn:schemas-microsoft-com:unattend">
"""

AUTOUNATTEND_PE = """  <settings pass="windowsPE">
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>5</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassStorageCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
"""

AUTOUNATTEND_NRO = """  <settings pass="specialize">
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
"""


def write_autounattend(usb_root, bypass_hw, local_account):
    existing = find_ci(Path(usb_root), "autounattend.xml")
    if existing is not None:
        log("Die ISO bringt bereits eine autounattend.xml mit – wird nicht überschrieben.")
        return
    xml = AUTOUNATTEND_HEAD
    if bypass_hw:
        xml += AUTOUNATTEND_PE
    if local_account:
        xml += AUTOUNATTEND_NRO
    xml += "</unattend>\n"
    path = os.path.join(usb_root, "autounattend.xml")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(xml)
        f.flush()
        os.fsync(f.fileno())
    what = []
    if bypass_hw:
        what.append("TPM/Secure-Boot/RAM/CPU-Prüfung umgehen")
    if local_account:
        what.append("Einrichtung ohne Microsoft-Konto")
    log("autounattend.xml geschrieben: " + ", ".join(what))


# --------------------------------------------------------------------------
# main job
# --------------------------------------------------------------------------

def scan_iso(root):
    files = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            rel = os.path.relpath(p, root)
            files.append((rel, sz))
            total += sz
    return files, total


def make_stick(iso, dev, bypass_hw=False, local_account=False, force_any=False):
    if os.geteuid() != 0:
        raise WorkerError("Root-Rechte benötigt.")
    iso = os.path.realpath(iso)
    if not os.path.isfile(iso):
        raise WorkerError("ISO-Datei nicht gefunden: %s" % iso)

    progress(0, "Prüfe Gerät", force=True)
    dsys = validate_target(dev, force_any)
    need_tool("mkfs.fat")

    work = tempfile.mkdtemp(prefix="winusb-", dir="/tmp")
    iso_mnt = os.path.join(work, "iso")
    usb_mnt = os.path.join(work, "usb")
    os.mkdir(iso_mnt)
    os.mkdir(usb_mnt)
    log("Arbeitsordner: %s (nur temporär, nichts im System)" % work)

    try:
        progress(1, "Öffne ISO", force=True)
        mount_iso(iso, iso_mnt)
        root = Path(iso_mnt)

        if find_ci(root, "efi", "boot", "bootx64.efi") is None:
            log("Warnung: efi/boot/bootx64.efi fehlt – Stick bootet evtl. nicht im UEFI-Modus.")
        sources = find_ci(root, "sources")
        wim = find_ci(root, "sources", "install.wim")
        esd = find_ci(root, "sources", "install.esd")
        if wim is None and esd is None and find_ci(root, "sources", "install.swm") is None:
            raise WorkerError("Das sieht nicht nach einer Windows-Installations-ISO aus "
                              "(sources/install.wim bzw. install.esd fehlt).")

        files, total = scan_iso(iso_mnt)
        split_rel = None
        for rel, sz in files:
            if sz > FAT32_MAX_FILE:
                if wim is not None and os.path.realpath(os.path.join(iso_mnt, rel)) == \
                        os.path.realpath(wim):
                    split_rel = rel
                else:
                    raise WorkerError("Die Datei %s ist größer als 4 GB und kann nicht auf FAT32 "
                                      "aufgeteilt werden." % rel)
        log("ISO: %d Dateien, %.2f GB" % (len(files), total / 1e9))

        progress(2, "Bereite Stick vor", force=True)
        unmount_all(dsys)
        check_cancel()

        log("Schreibe neue Partitionstabelle (MBR, FAT32) auf %s ..." % dev)
        size, ss, start, count = write_mbr(dev)
        if total + 64 * 1024 ** 2 > count * ss:
            raise WorkerError("Der Stick ist zu klein: %.1f GB benötigt, %.1f GB vorhanden." %
                              ((total + 64 * 1024 ** 2) / 1e9, count * ss / 1e9))
        part = reread_partitions(dev, dsys)
        unmount_all(dsys)       # desktop may have auto-mounted something
        progress(3, "Formatiere FAT32", force=True)
        log("Formatiere %s als FAT32 ..." % part)
        format_fat32(part)
        unmount_all(dsys)

        run([need_tool("mount"), "-t", "vfat", "-o", "rw,noatime,utf8,shortname=mixed", part, usb_mnt])
        check_cancel()

        prog = Progress(total)
        log("Kopiere Dateien ...")
        for rel, sz in files:
            check_cancel()
            src = os.path.join(iso_mnt, rel)
            dst = os.path.join(usb_mnt, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if rel == split_rel:
                continue
            copy_file(src, dst, prog, "Kopiere " + os.path.basename(rel))
        # empty directories
        for dirpath, dirnames, _ in os.walk(iso_mnt):
            for d in dirnames:
                os.makedirs(os.path.join(usb_mnt, os.path.relpath(os.path.join(dirpath, d), iso_mnt)),
                            exist_ok=True)

        if split_rel:
            dst_dir = os.path.join(usb_mnt, os.path.dirname(split_rel))
            split_wim(os.path.join(iso_mnt, split_rel), dst_dir, prog)

        if bypass_hw or local_account:
            write_autounattend(usb_mnt, bypass_hw, local_account)

        progress(97.5, "Schreibe Puffer auf den Stick", force=True)
        log("Schreibe restliche Daten auf den Stick (sync) ...")
        os.sync()
        progress(99, "Hänge aus", force=True)
        if not umount(usb_mnt, lazy_ok=False):
            os.sync()
            umount(usb_mnt)
        os.sync()
        progress(100, "Fertig", force=True)
        emit("done", msg="Fertig! Der Stick ist bereit und kann abgezogen werden.")
    finally:
        for m in (usb_mnt, iso_mnt):
            if is_mounted(m):
                umount(m)
        for m in (usb_mnt, iso_mnt, work):
            try:
                os.rmdir(m)
            except OSError:
                pass


def main():
    global JSON_MODE
    ap = argparse.ArgumentParser(
        prog="WinUSB-SteamOS",
        description="Bootfähigen Windows-USB-Stick aus einer ISO erstellen (läuft als root).")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--cli", action="store_true", help="Text-Modus (ohne GUI)")
    ap.add_argument("--iso", required=True, help="Pfad zur Windows-ISO")
    ap.add_argument("--device", required=True, help="Ziel-Gerät, z.B. /dev/sdc (wird GELÖSCHT)")
    ap.add_argument("--win11-bypass", action="store_true",
                    help="Windows 11: TPM-/Secure-Boot-/RAM-/CPU-Prüfung umgehen")
    ap.add_argument("--local-account", action="store_true",
                    help="Windows 11: Einrichtung ohne Microsoft-Konto erlauben")
    ap.add_argument("--yes", action="store_true", help="nicht nachfragen (CLI)")
    ap.add_argument("--force-any-device", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    JSON_MODE = args.worker
    signal.signal(signal.SIGTERM, lambda *a: CANCEL.set())
    signal.signal(signal.SIGHUP, lambda *a: CANCEL.set())
    if args.worker:
        watch_stdin()
    else:
        signal.signal(signal.SIGINT, lambda *a: CANCEL.set())
        if not args.yes:
            print("ACHTUNG: Alle Daten auf %s werden gelöscht!" % args.device)
            if input("Zum Fortfahren 'ja' eingeben: ").strip().lower() != "ja":
                print("Abgebrochen.")
                return 1

    try:
        make_stick(args.iso, args.device, args.win11_bypass, args.local_account,
                   args.force_any_device)
        if not JSON_MODE:
            print()
        return 0
    except Cancelled:
        emit("error", msg="Abgebrochen. Der Stick ist unvollständig und muss neu erstellt werden.")
        return 2
    except WorkerError as e:
        emit("error", msg=str(e))
        return 1
    except Exception as e:  # pragma: no cover
        import traceback
        emit("error", msg="Unerwarteter Fehler: %s\n%s" % (e, traceback.format_exc()[-1500:]))
        return 1


if __name__ == "__main__":
    sys.exit(main())
