#!/bin/bash
# End-to-end test of the built AppImage (runs in GitHub Actions, as root).
#
#  * builds a fake Windows ISO (UDF, like the real ones) with an install.wim > 4 GiB
#  * creates a fake 8 GB "USB stick" (loop device) with an old GPT/ext4 on it
#  * runs the AppImage in an Arch Linux container (SteamOS is Arch based)
#    WITHOUT FUSE, exactly like pkexec does it (APPIMAGE_EXTRACT_AND_RUN=1)
#  * checks partition table, filesystem, files, split WIM integrity, autounattend.xml
#  * starts the GUI on a virtual X server inside the Arch container
set -euo pipefail

APPIMAGE="$(readlink -f "${1:-dist/WinUSB-SteamOS-x86_64.AppImage}")"
WORK="${WORK:-/mnt/winusb-test}"
chmod +x "$APPIMAGE"

step() { printf '\n\033[1;32m### %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFAIL: %s\033[0m\n' "$*"; exit 1; }

step "Host tools"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq udftools wimtools dosfstools fdisk bzip2 zip >/dev/null
modprobe loop || true
modprobe udf || true
modprobe vfat || true

rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"

step "Fake Windows ISO with install.wim > 4 GiB"
mkdir -p tree/efi/boot tree/efi/microsoft/boot tree/boot tree/sources tree/support/logging big
head -c 1500000 /dev/urandom > tree/efi/boot/bootx64.efi
head -c 3000000 /dev/urandom > tree/efi/microsoft/boot/efisys.bin
head -c 300000  /dev/urandom > tree/boot/bcd
head -c 500000  /dev/urandom > tree/bootmgr.efi
head -c 400000  /dev/urandom > tree/setup.exe
echo "[AutoRun]" > tree/autorun.inf
head -c 20000000 /dev/urandom > tree/sources/boot.wim
# ~4.4 GB of incompressible data -> install.wim
for i in 1 2 3 4 5; do head -c 900000000 /dev/urandom > "big/data$i.bin"; done
wimlib-imagex capture big tree/sources/install.wim "Windows 11 Pro" --compress=none
rm -rf big
ls -l tree/sources/install.wim
WIMSIZE=$(stat -c %s tree/sources/install.wim)
[ "$WIMSIZE" -gt 4294967295 ] || fail "test wim not > 4 GiB"

truncate -s 5200M test.iso
mkudffs --utf8 --media-type=hd --blocksize=2048 --label=CCCOMA_X64FRE test.iso >/dev/null
mkdir -p isomnt
mount -o loop test.iso isomnt
cp -r tree/. isomnt/
umount isomnt
sha256sum tree/efi/boot/bootx64.efi tree/sources/boot.wim > sums.txt
rm -rf tree/sources/install.wim

step "Fake 8 GB USB stick with old GPT + ext4 on it"
truncate -s 8G usb.img
LOOP=$(losetup -fP --show usb.img)
echo "loop device: $LOOP"
echo 'label: gpt
,1G,L
,,L' | sfdisk -q "$LOOP"
partx -u "$LOOP" || true
sleep 1
mkfs.ext4 -q "${LOOP}p1" || true
cleanup() { umount "$WORK/check" 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; }
trap cleanup EXIT

step "Run AppImage worker inside Arch Linux (no FUSE, like pkexec)"
docker run --rm --privileged -v /dev:/dev -v "$WORK:$WORK" -v "$APPIMAGE:/app/WinUSB.AppImage:ro" \
    archlinux:latest bash -euc "
        cat /etc/os-release | head -2
        ldd --version | head -1
        cd /tmp
        APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --cli --iso '$WORK/test.iso' --device '$LOOP' \
            --yes --force-any-device --win11-bypass --local-account
    "

step "Verify result"
partx -u "$LOOP" 2>/dev/null || true
blockdev --rereadpt "$LOOP" 2>/dev/null || true
sleep 1
sfdisk -d "$LOOP"
sfdisk -d "$LOOP" | grep -q 'type=c, bootable' || fail "partition is not FAT32-LBA + bootable"
sfdisk -d "$LOOP" | grep -c '^/dev' | grep -qx 1 || fail "expected exactly one partition"
[ "$(blkid -o value -s TYPE "${LOOP}p1")" = vfat ] || fail "not vfat"
blkid "${LOOP}p1"
fsck.fat -n "${LOOP}p1" || fail "fsck.fat reported errors"

mkdir -p check
mount -o ro "${LOOP}p1" check
ls -la check check/sources
( cd check && sed 's#tree/##' "$WORK/sums.txt" | sha256sum -c ) || fail "checksum mismatch"
[ -f check/setup.exe ] && [ -f check/efi/boot/bootx64.efi ] || fail "files missing"
[ ! -e check/sources/install.wim ] || fail "install.wim should have been split"
ls check/sources/install*.swm
[ "$(ls check/sources/install*.swm | wc -l)" -ge 2 ] || fail "expected >= 2 swm parts"
for f in check/sources/install*.swm; do
    [ "$(stat -c %s "$f")" -lt 4294967295 ] || fail "$f too big for FAT32"
done
wimlib-imagex verify check/sources/install.swm --ref="check/sources/install*.swm" || fail "split WIM corrupt"
wimlib-imagex info check/sources/install.swm | grep -q "Windows 11 Pro" \
    || fail "image name lost"
grep -q BypassTPMCheck check/autounattend.xml || fail "autounattend bypass missing"
grep -q BypassNRO check/autounattend.xml || fail "autounattend NRO missing"
umount check

step "Worker JSON mode + safety checks (Arch container)"
docker run --rm --privileged -v /dev:/dev -v "$WORK:$WORK" -v "$APPIMAGE:/app/WinUSB.AppImage:ro" \
    archlinux:latest bash -uc "
        cd /tmp
        echo '>> refusing a partition instead of a disk:'
        out=\$(sleep 30 | APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --worker --iso '$WORK/test.iso' --device '${LOOP}p1' --force-any-device)
        echo \"\$out\"
        echo \"\$out\" | grep -q '\"t\": \"error\"' || exit 1
        echo '>> refusing a non-removable disk without override:'
        out=\$(sleep 30 | APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --worker --iso '$WORK/test.iso' --device '$LOOP')
        echo \"\$out\"
        echo \"\$out\" | grep -q 'USB' || exit 1
        echo '>> cancel via closed stdin:'
        out=\$(APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --worker --iso '$WORK/test.iso' --device '$LOOP' --force-any-device < /dev/null)
        echo \"\$out\" | tail -2
        echo \"\$out\" | grep -q 'Abgebrochen' || exit 1
    " || fail "safety checks"

step "Image mode (SteamOS recovery style .img.bz2 / .zip, Linux hybrid ISO detection)"
# 600 MB disk image with GPT + 2 partitions, like steamdeck-repair-*.img
truncate -s 600M steamos.img
echo 'label: gpt
,64M,U
,,L' | sfdisk -q steamos.img
head -c 50000000 /dev/urandom | dd of=steamos.img bs=1M seek=100 conv=notrunc status=none
bzip2 -k -1 steamos.img
( zip -q -1 steamos.zip steamos.img ) 2>/dev/null || python3 -c "
import zipfile; z=zipfile.ZipFile('steamos.zip','w',zipfile.ZIP_DEFLATED); z.write('steamos.img'); z.close()"
IMGSIZE=$(stat -c %s steamos.img)
for img in steamos.img.bz2 steamos.zip; do
    echo ">> writing $img"
    docker run --rm --privileged -v /dev:/dev -v "$WORK:$WORK" -v "$APPIMAGE:/app/WinUSB.AppImage:ro" \
        archlinux:latest bash -euc "
            cd /tmp
            [ \"\$(APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --cli --iso '$WORK/$img' --device '$LOOP' --detect)\" = raw ]
            APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --cli --iso '$WORK/$img' --device '$LOOP' --yes --force-any-device
        " || fail "image mode $img"
    cmp -n "$IMGSIZE" steamos.img "$LOOP" || fail "image content differs ($img)"
    partx -u "$LOOP" 2>/dev/null || true; sleep 1
    sfdisk -d "$LOOP" | grep -q 'label: gpt' || fail "GPT not visible after writing $img"
    echo "   OK: identical, GPT visible"
done
# hybrid ISO is detected as raw, Windows (UDF) ISO as windows
cp steamos.img hybrid.iso
printf '\x80' | dd of=hybrid.iso bs=1 seek=446 conv=notrunc status=none
docker run --rm -v "$WORK:$WORK" -v "$APPIMAGE:/app/WinUSB.AppImage:ro" archlinux:latest bash -euc "
    cd /tmp
    a=\$(APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --cli --iso '$WORK/hybrid.iso' --device x --detect)
    b=\$(APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --cli --iso '$WORK/test.iso' --device x --detect)
    echo \"hybrid.iso -> \$a, test.iso -> \$b\"
    [ \"\$a\" = raw ] && [ \"\$b\" = windows ]
" || fail "mode detection"

step "GUI on a virtual X server inside Arch Linux"
cat > "$WORK/gui_test.sh" <<'EOS'
#!/bin/bash
set -u
pacman -Sy --noconfirm --needed xorg-server-xvfb xorg-xauth fontconfig ttf-dejavu mesa libglvnd \
    libxkbcommon libx11 libxcb dbus glib2 xorg-xkbcomp xkeyboard-config >/dev/null
cd /tmp
/app/WinUSB.AppImage --appimage-extract >/dev/null || exit 1
echo 'missing libraries of the Qt xcb plugin (should be none):'
export LD_LIBRARY_PATH=/tmp/squashfs-root/usr/lib/gui
for f in squashfs-root/opt/python/lib/python3*/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so \
         squashfs-root/opt/python/lib/python3*/site-packages/PySide6/Qt/lib/libQt6XcbQpa.so.6; do
    ldd "$f" | grep 'not found'
done
unset LD_LIBRARY_PATH
echo '--- offscreen ---'
QT_QPA_PLATFORM=offscreen ./squashfs-root/AppRun --selftest; echo "rc=$?"
echo '--- xcb on Xvfb ---'
command -v Xvfb || exit 1
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.2; done
ls -la /tmp/.X11-unix/ || cat /tmp/xvfb.log
DISPLAY=:99 QT_QPA_PLATFORM=xcb ./squashfs-root/AppRun --selftest > /tmp/gui.log 2>&1
rc=$?
echo "rc=$rc"
cat /tmp/gui.log
if [ "$rc" -ge 128 ]; then
    echo '--- crash diagnostics ---'
    PY=$(ls -d squashfs-root/opt/python/bin/python3)
    G=/tmp/squashfs-root/usr/lib/gui
    echo '>> python faulthandler:'
    DISPLAY=:99 QT_QPA_PLATFORM=xcb LD_LIBRARY_PATH=$G "$PY" -X faulthandler -s -E squashfs-root/usr/share/winusb/winusb_gui.py --selftest 2>&1 | tail -30
    echo '>> minimal Qt window:'
    DISPLAY=:99 QT_QPA_PLATFORM=xcb LD_LIBRARY_PATH=$G "$PY" -X faulthandler -c "
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import QTimer
a=QApplication([]); l=QLabel('hi'); l.show(); QTimer.singleShot(500,a.quit); a.exec(); print('MINIMAL_OK')" 2>&1 | tail -15
    echo '>> with Arch xcb-util libs instead of bundled:'
    pacman -S --noconfirm --needed xcb-util xcb-util-cursor xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-wm libsm libice 2>&1 | tail -1
    DISPLAY=:99 QT_QPA_PLATFORM=xcb "$PY" -X faulthandler -s -E squashfs-root/usr/share/winusb/winusb_gui.py --selftest 2>&1 | tail -15
    echo '>> gdb:'
    pacman -S --noconfirm --needed gdb 2>&1 | tail -1
    DISPLAY=:99 QT_QPA_PLATFORM=xcb LD_LIBRARY_PATH=$G gdb -q -batch -ex run -ex bt \
        --args "$PY" -s -E squashfs-root/usr/share/winusb/winusb_gui.py --selftest 2>&1 | grep -E '^#|signal' | head -25
fi
grep -q 'GUI_SELFTEST_OK.*platform=xcb' /tmp/gui.log || exit 1
echo '--- same through the AppImage runtime (extract-and-run) ---'
DISPLAY=:99 QT_QPA_PLATFORM=xcb APPIMAGE_EXTRACT_AND_RUN=1 /app/WinUSB.AppImage --selftest 2>&1 | tee /tmp/gui2.log
grep -q 'GUI_SELFTEST_OK.*platform=xcb' /tmp/gui2.log
EOS
docker run --rm -v "$APPIMAGE:/app/WinUSB.AppImage:ro" -v "$WORK/gui_test.sh:/gui_test.sh:ro" \
    archlinux:latest bash /gui_test.sh || fail "GUI did not start"

step "AppImage via FUSE on the host (normal double-click start)"
"$APPIMAGE" --version || echo "(FUSE not available on this runner – not an error)"

step "ALL TESTS PASSED"
