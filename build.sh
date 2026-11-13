#!/bin/bash
# Build WinUSB-SteamOS-x86_64.AppImage
#
# Intended to run on Ubuntu 22.04 (or in an ubuntu:22.04 container) as root
# or with the packages from .github/workflows/build.yml installed.
# Result: dist/WinUSB-SteamOS-x86_64.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"
PYVER="${PYVER:-3.12}"
PYSIDE_SPEC="${PYSIDE_SPEC:-PySide6-Essentials>=6.8,<6.9}"
DOSFS_VER="${DOSFS_VER:-4.2}"
WIMLIB_VER="${WIMLIB_VER:-1.14.4}"
OUTNAME="WinUSB-SteamOS-x86_64.AppImage"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

rm -rf "$BUILD"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share/winusb" \
         "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
         "$APPDIR/opt" "$DIST" "$BUILD/src"

# ---------------------------------------------------------------- Python
log "Portable Python $PYVER (python-build-standalone via uv)"
export UV_PYTHON_INSTALL_DIR="$BUILD/pythons"
uv python install "$PYVER"
PYDIR="$(find "$UV_PYTHON_INSTALL_DIR" -maxdepth 1 -type d -name "cpython-${PYVER}.*-linux-x86_64-gnu" | sort -V | tail -1)"
[ -n "$PYDIR" ] || { echo "Python not found"; exit 1; }
cp -a "$PYDIR" "$APPDIR/opt/python"
find "$APPDIR/opt/python" -name EXTERNALLY-MANAGED -delete
PY="$APPDIR/opt/python/bin/python3"
"$PY" --version

log "PySide6 ($PYSIDE_SPEC)"
uv pip install --python "$PY" --break-system-packages --no-cache "$PYSIDE_SPEC"
SP="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PS="$SP/PySide6"

log "Trimming unused parts"
# Python
PYLIB="$APPDIR/opt/python/lib/python$PYVER"
rm -rf "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" \
       "$PYLIB/ensurepip" "$PYLIB/lib2to3" "$PYLIB"/lib-dynload/_tkinter* \
       "$APPDIR/opt/python/lib"/{libtcl*,libtk*,tcl8*,tk8*,itcl*,thread*,Tix*} \
       "$APPDIR/opt/python/include" "$APPDIR/opt/python/share"
rm -rf "$SP"/pip "$SP"/pip-*
# Qt / PySide6: only Core/Gui/Widgets (+ what the platform plugins need) are used
rm -rf "$PS"/{examples,include,glue,typesystems,scripts,doc} \
       "$PS"/{designer,assistant,linguist,lupdate,lrelease,rcc,uic,qmllint,qmlformat,qmlls,balsam,balsamui,qsb,svgtoqml} \
       "$PS"/Qt/{qml,metatypes} "$PS"/Qt/plugins/{qmltooling,designer,qmllint,qmlls} 2>/dev/null || true
for m in Qml Quick QuickControls2 QuickWidgets QuickTest Designer Pdf PdfWidgets UiTools \
         Quick3D ShaderTools Sql Test Concurrent Help; do
    rm -f "$PS"/Qt${m}.*so* "$PS"/Qt${m}.pyi
    rm -f "$PS"/Qt/lib/libQt6${m}*.so*
done
rm -f "$PS"/Qt/lib/libQt6Labs*.so* "$PS"/Qt/lib/libQt6QmlWorkerScript*.so* "$PS"/Qt/lib/libQt6QuickTemplates*.so*
if [ -d "$PS/Qt/translations" ]; then
    find "$PS/Qt/translations" -type f ! -name 'qtbase_de.qm' ! -name 'qt_de.qm' -delete
fi

# xcb helper libs the Qt xcb plugin needs but the wheel does not ship
log "Bundling xcb-util libraries for the Qt xcb plugin"
mkdir -p "$APPDIR/usr/lib/gui"
for lib in libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-keysyms.so.1 libxcb-image.so.0 \
           libxcb-render-util.so.0 libxcb-util.so.1 libxkbcommon-x11.so.0 libxkbcommon.so.0 \
           libSM.so.6 libICE.so.6 libxcb-xinerama.so.0 libxcb-xinput.so.0; do
    src="$(ldconfig -p | awk -v l="$lib" '$1==l && /x86-64/ {print $NF; exit}')"
    if [ -n "$src" ]; then
        # separate dir, added to LD_LIBRARY_PATH by AppRun for the GUI only
        # (no patchelf: old patchelf versions are known to corrupt libraries)
        cp -L "$src" "$APPDIR/usr/lib/gui/$lib"
        echo "  + $lib"
    else
        echo "  ! $lib not found on build host (skipped)"
    fi
done

# ---------------------------------------------------------------- tools
log "mkfs.fat (dosfstools $DOSFS_VER, static)"
cd "$BUILD/src"
curl -fsSL -o dosfstools.tar.gz \
    "https://github.com/dosfstools/dosfstools/releases/download/v${DOSFS_VER}/dosfstools-${DOSFS_VER}.tar.gz"
tar xzf dosfstools.tar.gz
( cd "dosfstools-${DOSFS_VER}" && ./configure -q LDFLAGS=-static >/dev/null && make -s -j"$(nproc)" >/dev/null )
install -m755 "dosfstools-${DOSFS_VER}/src/mkfs.fat" "$APPDIR/usr/bin/mkfs.fat"
strip "$APPDIR/usr/bin/mkfs.fat"

log "wimlib-imagex"
WIMOK=0
if curl -fsSL -o wimlib.tar.gz "https://wimlib.net/downloads/wimlib-${WIMLIB_VER}.tar.gz"; then
    tar xzf wimlib.tar.gz
    if ( cd "wimlib-${WIMLIB_VER}" && \
         ./configure -q --without-ntfs-3g --without-fuse --disable-shared --enable-static >/dev/null && \
         make -s -j"$(nproc)" >/dev/null ); then
        bin="wimlib-${WIMLIB_VER}/wimlib-imagex"
        [ -f "wimlib-${WIMLIB_VER}/.libs/wimlib-imagex" ] && bin="wimlib-${WIMLIB_VER}/.libs/wimlib-imagex"
        install -m755 "$bin" "$APPDIR/usr/bin/wimlib-imagex"
        strip "$APPDIR/usr/bin/wimlib-imagex"
        WIMOK=1
        echo "  built from source (no extra libraries)"
    fi
fi
if [ "$WIMOK" != 1 ]; then
    echo "  source build failed -> using distro wimlib-imagex + bundled libraries"
    install -m755 "$(command -v wimlib-imagex)" "$APPDIR/usr/bin/wimlib-imagex"
    ldd "$APPDIR/usr/bin/wimlib-imagex" | awk '/=> \//{print $3}' | while read -r so; do
        case "$(basename "$so")" in
            libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|libresolv.so*|ld-linux*) continue ;;
        esac
        cp -L "$so" "$APPDIR/usr/lib/"
    done
    patchelf --set-rpath '$ORIGIN/../lib' "$APPDIR/usr/bin/wimlib-imagex"
    for so in "$APPDIR"/usr/lib/*.so*; do patchelf --set-rpath '$ORIGIN' "$so"; done
fi
ldd "$APPDIR/usr/bin/wimlib-imagex" || true
"$APPDIR/usr/bin/wimlib-imagex" --version | head -1
"$APPDIR/usr/bin/mkfs.fat" --help 2>&1 | head -1 || true

# ---------------------------------------------------------------- app
log "Application files"
cd "$ROOT"
install -m644 src/winusb_gui.py src/winusb_worker.py "$APPDIR/usr/share/winusb/"
APP_VERSION="${APP_VERSION:-dev}"
BUILD_DATE="$(date -u +%Y-%m-%d)"
BUILD_COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"
printf '{"version": "%s", "date": "%s", "commit": "%s"}\n' "$APP_VERSION" "$BUILD_DATE" "$BUILD_COMMIT" \
    > "$APPDIR/usr/share/winusb/build_info.json"
cat "$APPDIR/usr/share/winusb/build_info.json"
install -m755 packaging/AppRun "$APPDIR/AppRun"
install -m644 packaging/winusb-steamos.desktop "$APPDIR/winusb-steamos.desktop"
install -m644 packaging/winusb-steamos.desktop "$APPDIR/usr/share/applications/winusb-steamos.desktop"
QT_QPA_PLATFORM=offscreen "$PY" packaging/make_icon.py "$APPDIR/winusb-steamos.png"
cp "$APPDIR/winusb-steamos.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/winusb-steamos.png"
ln -sf winusb-steamos.png "$APPDIR/.DirIcon"

log "Self-check of the AppDir"
QT_QPA_PLATFORM=offscreen "$APPDIR/AppRun" --selftest
"$APPDIR/AppRun" --cli --help | head -3

# ---------------------------------------------------------------- AppImage
log "appimagetool"
if [ ! -x "$BUILD/appimagetool" ]; then
    curl -fsSL -o "$BUILD/appimagetool" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$BUILD/appimagetool"
fi
rm -f "$DIST/$OUTNAME"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$BUILD/appimagetool" -n "$APPDIR" "$DIST/$OUTNAME"
chmod +x "$DIST/$OUTNAME"
ls -lh "$DIST/$OUTNAME"
log "Done: $DIST/$OUTNAME"
