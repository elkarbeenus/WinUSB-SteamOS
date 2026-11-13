# WinUSB-SteamOS

Create bootable USB sticks directly on **SteamOS** (Desktop Mode), the Steam Deck and other Linux systems:

- **Windows 10/11** installer sticks from the official ISO (like Rufus / WoeUSB)
- **SteamOS recovery image** from Valve (`steamdeck-repair-….img.bz2` / `.zip`) and **Linux ISOs** (Ubuntu, Bazzite, Fedora …), written 1:1 and verified (like Etcher)

One single AppImage. Nothing to install, no `steamos-readonly disable`, no pacman.
The interface is available in **English and German** (picked automatically from your system language, switchable in the app).

![build](https://github.com/elkarbeenus/WinUSB-SteamOS/actions/workflows/build.yml/badge.svg)

## Why not WoeUSB-ng?

WoeUSB-ng (including its AppImage) mounts the ISO under `/media/…`. On SteamOS the root filesystem is read-only, so it fails with
`mount point directory /media/woeusb_source_… could not be created`.

WinUSB-SteamOS only mounts inside a **temporary folder under `/tmp`** and never writes to the system.

| | WinUSB-SteamOS | WoeUSB-ng AppImage |
|---|---|---|
| Works on read-only SteamOS | ✅ | ❌ (`/media`) |
| Required system packages | none (wimlib + mkfs.fat are bundled) | wimlib, parted, grub … |
| Windows 11 bypass (TPM / Secure Boot / RAM) | ✅ like Rufus | ❌ |
| install.wim > 4 GB | split into `install.swm` parts | ✅ |
| SteamOS / Linux images (1:1 + verify) | ✅ | ❌ |
| English / German UI | ✅ | – |

## Usage

1. Download `WinUSB-SteamOS-x86_64.AppImage` from [**Releases**](https://github.com/elkarbeenus/WinUSB-SteamOS/releases/latest).
2. Right-click → *Properties* → *Permissions* → tick **Is executable**.
3. Double-click → choose an ISO/image (or drop it onto the window) → choose the USB stick → **Create stick**.
   The mode is detected automatically: Windows ISO → Windows installer, everything else → written 1:1.
4. Enter your password (the system asks for it; it is needed to write to the stick).

Optional: **When finished → Sleep / Shut down this device** (with a 30-second countdown you can cancel).

> **Steam Deck:** the `deck` user has **no password** out of the box. Open Konsole once, run `passwd` and set one. Otherwise the password prompt can't be confirmed.

**Booting:** open the boot menu when the target PC starts (usually F12 / F11 / F8 / Esc).
Steam Deck: hold **Volume down** and press **Power**.

### SteamOS stick (recovery / installation)

Download the recovery image from Valve (*help.steampowered.com → Steam Deck Recovery Image*) and choose the `.bz2` or `.zip` file directly. There is no need to extract it. Click **Create stick**. Afterwards the stick is read back and compared automatically.

### Text mode (terminal)

```bash
sudo ./WinUSB-SteamOS-x86_64.AppImage --cli --iso ~/Downloads/Win11.iso --device /dev/sdX \
     [--win11-bypass] [--local-account] [--lang en|de]

# write an image 1:1 (e.g. SteamOS recovery)
sudo ./WinUSB-SteamOS-x86_64.AppImage --cli --image steamdeck-repair.img.bz2 --device /dev/sdX
```

`./WinUSB-SteamOS-x86_64.AppImage --version` shows the version and build date.

## How it works

### Windows mode

Based on Rufus (FAT32 mode) and WoeUSB-ng:

1. The ISO is mounted read-only via a loop device under `/tmp/winusb-XXXX/iso` (UDF).
2. Old partition tables are wiped, including the GPT backup at the end. A new **MBR** with **one FAT32 partition** (type `0x0C`, active) is written directly by the program, without parted or sfdisk.
3. `mkfs.fat -F 32` (bundled).
4. All files are copied with regular `fdatasync` calls, so the progress bar is honest and there is no endless "sync" at the end.
5. An `install.wim` larger than 4 GB is split with **wimlib** into `install.swm`, `install2.swm` …, which Windows Setup supports natively.
6. Optional: `autounattend.xml` with the Rufus tweaks (LabConfig bypass, BypassNRO).

Windows sticks boot via **UEFI**: all PCs since about 2012 and the Steam Deck. Windows 11 requires UEFI anyway.

### Image mode

The file is decompressed on the fly (bzip2, xz, gzip, zip) and written block by block while a SHA-256 checksum is calculated. Afterwards the stick is read back and compared (can be turned off). Hybrid ISOs (with an MBR, e.g. Ubuntu) are detected automatically.

### Privileges and the AppImage

The interface runs as your normal user. To write to the stick it starts itself again through
`pkexec env APPIMAGE_EXTRACT_AND_RUN=1 <AppImage> --worker …`: root cannot look into another user's FUSE mount. That is a typical reason why other AppImages fail with pkexec.
To cancel, the GUI simply closes the root process's input.

Safety checks: only whole USB/removable drives; no devices with a mounted system (`/`, `/home`, `/var` …); no LVM/RAID/LUKS devices.

## Building

```bash
# Ubuntu 22.04 (or a container), needs uv, patchelf, build-essential
APP_VERSION=1.0.0 ./build.sh
# -> dist/WinUSB-SteamOS-x86_64.AppImage
```

Contents: portable Python (python-build-standalone), PySide6 (Qt 6), statically built `mkfs.fat`, `wimlib-imagex`, AppImage runtime (static, does not need libfuse2).

## CI tests

Every push builds the AppImage and tests it (see `tests/ci_test.sh`):

- a real UDF ISO like Microsoft's, with an `install.wim` > 4 GB
- an 8 GB "stick" (loop device) with an old GPT + ext4 on it
- runs inside an **Arch Linux container** (SteamOS is based on Arch), **without FUSE**, exactly like via pkexec
- checks: partition table, `fsck.fat`, checksums, `wimlib-imagex verify` of the split parts, autounattend.xml
- image mode with `.img.bz2` and `.zip` (like the SteamOS recovery image): byte-for-byte comparison, GPT visible
- safety checks (partition instead of disk, internal drive, cancel)
- GUI start on a virtual X server under Arch, including the language switch

## Credits

Created by **Dominic Lesina × Claude**.

## License

MIT. Bundled software: wimlib (LGPLv3+/GPLv3+), dosfstools (GPLv3+), Qt/PySide6 (LGPLv3), Python (PSF).
