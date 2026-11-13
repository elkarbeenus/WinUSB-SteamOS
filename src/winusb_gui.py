#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""WinUSB-SteamOS – graphical front end (runs as normal user).

The actual writing is done by winusb_worker.py, started with root rights
through pkexec. When running from the AppImage the AppImage itself is
started via `pkexec env APPIMAGE_EXTRACT_AND_RUN=1 <AppImage> --worker ...`
so root never has to read the user's FUSE mount (which it can't).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget, QToolButton,
)

APP_NAME = "WinUSB-SteamOS"
VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
WORKER = HERE / "winusb_worker.py"
sys.path.insert(0, str(HERE))
from winusb_worker import detect_mode  # noqa: E402


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else "%d B" % n
        n /= 1000.0


def find_exe(name):
    for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return shutil.which(name)


SYSTEM_MOUNT_PREFIXES = ("/", "/home", "/var", "/usr", "/boot", "/efi", "/etc", "/opt")


def list_usb_devices():
    """Return removable/USB disks that do not carry the running system."""
    lsblk = find_exe("lsblk") or "lsblk"
    try:
        out = subprocess.run(
            [lsblk, "-J", "-b", "-o",
             "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINT,LABEL,RO"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10).stdout
        data = json.loads(out or "{}")
    except Exception:
        return []

    def flag(v):
        return v in (True, 1, "1", "true")

    def mounts(node):
        res = []
        if node.get("mountpoint"):
            res.append(node["mountpoint"])
        for c in node.get("children") or []:
            res.extend(mounts(c))
        return res

    devs = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk" or flag(d.get("ro")):
            continue
        size = int(d.get("size") or 0)
        if size <= 0:
            continue
        name = d.get("name", "")
        if name.startswith(("loop", "zram", "sr", "ram", "nbd")):
            continue
        tran = (d.get("tran") or "").lower()
        removable = tran == "usb" or flag(d.get("hotplug")) or flag(d.get("rm"))
        if not removable:
            continue
        mps = mounts(d)
        if any(m in SYSTEM_MOUNT_PREFIXES or m.startswith(("/usr/", "/var/", "/home/", "/boot/"))
               for m in mps if m and not m.startswith("/run/media/")):
            continue
        model = " ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x)
        labels = [c.get("label") for c in (d.get("children") or []) if c.get("label")]
        path = d.get("path") or ("/dev/" + name)
        kind = "SD-Karte" if name.startswith("mmcblk") else "USB"
        text = "%s  –  %s  (%s)" % (model or kind, human(size), path)
        if labels:
            text += "  [%s]" % ", ".join(labels)
        devs.append({"path": path, "size": size, "text": text})
    return devs


class MainWindow(QMainWindow):
    def __init__(self, iso=None):
        super().__init__()
        self.setWindowTitle("%s – bootfähigen USB-Stick erstellen" % APP_NAME)
        self.proc = None
        self.devices = []
        self.finished_ok = False
        self.last_error = None
        self.cancel_requested = False

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setSpacing(12)
        lay.setContentsMargins(16, 16, 16, 16)

        title = QLabel("<b style='font-size:15pt'>Bootfähigen USB-Stick erstellen</b><br>"
                       "<span style='color:gray'>Windows 10/11-ISO, SteamOS-Recovery-Image oder "
                       "Linux-ISO – direkt unter SteamOS.</span>")
        title.setTextFormat(Qt.RichText)
        lay.addWidget(title)

        # --- ISO ---
        g1 = QGroupBox("1. ISO / Image")
        v1 = QVBoxLayout(g1)
        h1 = QHBoxLayout()
        v1.addLayout(h1)
        self.iso_edit = QLineEdit()
        self.iso_edit.setPlaceholderText("Datei auswählen oder hierher ziehen (.iso, .img, .img.bz2, .zip …)")
        self.iso_edit.setReadOnly(True)
        self.iso_btn = QPushButton("Auswählen …")
        self.iso_btn.clicked.connect(self.choose_iso)
        h1.addWidget(self.iso_edit, 1)
        h1.addWidget(self.iso_btn)
        hm = QHBoxLayout()
        hm.addWidget(QLabel("Modus:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Automatisch erkennen", "auto")
        self.mode_combo.addItem("Windows-Installer (ISO → FAT32, wie Rufus)", "windows")
        self.mode_combo.addItem("Image 1:1 schreiben (SteamOS, Linux – wie Etcher/dd)", "raw")
        self.mode_combo.currentIndexChanged.connect(self.update_mode)
        hm.addWidget(self.mode_combo, 1)
        v1.addLayout(hm)
        self.mode_hint = QLabel()
        self.mode_hint.setStyleSheet("color: gray")
        self.mode_hint.setWordWrap(True)
        v1.addWidget(self.mode_hint)
        lay.addWidget(g1)

        # --- device ---
        g2 = QGroupBox("2. USB-Stick")
        v2 = QVBoxLayout(g2)
        h2 = QHBoxLayout()
        self.dev_combo = QComboBox()
        self.dev_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_btn = QPushButton("Aktualisieren")
        self.refresh_btn.clicked.connect(lambda: self.refresh_devices(force=True))
        h2.addWidget(self.dev_combo, 1)
        h2.addWidget(self.refresh_btn)
        v2.addLayout(h2)
        self.dev_hint = QLabel()
        self.dev_hint.setStyleSheet("color: gray")
        v2.addWidget(self.dev_hint)
        lay.addWidget(g2)

        # --- options ---
        g3 = QGroupBox("3. Optionen")
        v3 = QVBoxLayout(g3)
        self.g3 = g3
        self.opt_bypass = QCheckBox("TPM-, Secure-Boot-, RAM- und CPU-Prüfung umgehen "
                                    "(für ältere PCs)")
        self.opt_local = QCheckBox("Einrichtung ohne Microsoft-Konto erlauben (lokales Konto)")
        v3.addWidget(self.opt_bypass)
        v3.addWidget(self.opt_local)
        self.opt_verify = QCheckBox("Nach dem Schreiben überprüfen (empfohlen)")
        self.opt_verify.setChecked(True)
        v3.addWidget(self.opt_verify)
        note = QLabel("Windows-Sticks booten im UEFI-Modus (alle PCs ab ca. 2012 und das Steam Deck).")
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)
        v3.addWidget(note)
        lay.addWidget(g3)

        # --- progress ---
        self.status = QLabel("Bereit.")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        lay.addWidget(self.status)
        lay.addWidget(self.bar)

        self.log_toggle = QToolButton()
        self.log_toggle.setText("Details anzeigen")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self.toggle_log)
        lay.addWidget(self.log_toggle, 0, Qt.AlignLeft)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setVisible(False)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.log.setFont(f)
        lay.addWidget(self.log, 1)

        # --- buttons ---
        hb = QHBoxLayout()
        self.about_btn = QPushButton("Info")
        self.about_btn.clicked.connect(self.about)
        self.cancel_btn = QPushButton("Abbrechen")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setEnabled(False)
        self.start_btn = QPushButton("Stick erstellen")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 22px;")
        self.start_btn.clicked.connect(self.start)
        hb.addWidget(self.about_btn)
        hb.addStretch(1)
        hb.addWidget(self.cancel_btn)
        hb.addWidget(self.start_btn)
        lay.addLayout(hb)

        lay.addStretch(0)
        self.setAcceptDrops(True)
        self.resize(760, 560)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_devices)
        self.timer.start(2500)
        self.refresh_devices(force=True)
        if iso:
            self.set_iso(iso)
        self.update_mode()
        self.update_buttons()

    # ---------------------------------------------------------------- ui
    def toggle_log(self, on):
        self.log.setVisible(on)
        self.log_toggle.setText("Details ausblenden" if on else "Details anzeigen")

    def append_log(self, text):
        self.log.appendPlainText(text)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p):
                self.set_iso(p)
                break

    def choose_iso(self):
        start = os.path.dirname(self.iso_edit.text()) if self.iso_edit.text() else \
            str(Path.home() / "Downloads") if (Path.home() / "Downloads").is_dir() else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "ISO oder Image auswählen", start,
            "ISO und Images (*.iso *.ISO *.img *.IMG *.raw *.bz2 *.xz *.gz *.zip);;"
            "Windows-ISO (*.iso *.ISO);;Alle Dateien (*)")
        if path:
            self.set_iso(path)

    def set_iso(self, path):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return
        self.iso_edit.setText(path)
        self.iso_edit.setToolTip("%s (%s)" % (path, human(os.path.getsize(path))))
        self.update_mode()
        self.update_buttons()

    def effective_mode(self):
        m = self.mode_combo.currentData()
        if m != "auto":
            return m
        p = self.iso_edit.text()
        return detect_mode(p) if p else "windows"

    def update_mode(self, *_):
        p = self.iso_edit.text()
        mode = self.effective_mode()
        win = mode == "windows"
        self.opt_bypass.setVisible(win)
        self.opt_local.setVisible(win)
        self.opt_verify.setVisible(not win)
        if not p:
            self.mode_hint.setText("")
        elif win:
            self.mode_hint.setText("→ Windows-Installations-Stick: FAT32, große install.wim wird "
                                   "automatisch aufgeteilt.")
        else:
            self.mode_hint.setText("→ Wird 1:1 auf den Stick geschrieben (z.B. SteamOS-Recovery-Image, "
                                   "Ubuntu, Bazzite). Komprimierte Dateien werden direkt entpackt.")

    def refresh_devices(self, force=False):
        if self.proc is not None:
            return
        devs = list_usb_devices()
        if not force and [d["path"] + str(d["size"]) for d in devs] == \
                [d["path"] + str(d["size"]) for d in self.devices]:
            return
        current = self.dev_combo.currentData()
        self.devices = devs
        self.dev_combo.clear()
        for d in devs:
            self.dev_combo.addItem(d["text"], d["path"])
        if current:
            idx = self.dev_combo.findData(current)
            if idx >= 0:
                self.dev_combo.setCurrentIndex(idx)
        if devs:
            self.dev_hint.setText("Achtung: Alle Daten auf dem gewählten Stick werden gelöscht.")
        else:
            self.dev_hint.setText("Kein USB-Stick gefunden – einfach einstecken, er erscheint "
                                  "automatisch (mind. 8 GB).")
        self.update_buttons()

    def update_buttons(self):
        busy = self.proc is not None
        self.start_btn.setEnabled(not busy and bool(self.iso_edit.text()) and
                                  self.dev_combo.count() > 0)
        for w in (self.iso_btn, self.dev_combo, self.refresh_btn, self.opt_bypass, self.opt_local,
                  self.opt_verify, self.mode_combo):
            w.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)

    def about(self):
        QMessageBox.about(
            self, "Info",
            "<b>%s %s</b><br><br>Erstellt bootfähige Windows-Installations-Sticks unter "
            "SteamOS und anderen Linux-Systemen – ohne etwas zu installieren und ohne "
            "Schreibzugriff auf das System.<br><br>"
            "Methode: MBR + FAT32, große install.wim wird mit wimlib in install.swm-Teile "
            "aufgeteilt (wie Rufus/WoeUSB).<br><br>Lizenz: MIT" % (APP_NAME, VERSION))

    # ------------------------------------------------------------ run
    def build_command(self, iso, dev):
        mode = self.effective_mode()
        args = ["--worker", "--iso", iso, "--device", dev, "--mode", mode]
        if mode == "windows":
            if self.opt_bypass.isChecked():
                args.append("--win11-bypass")
            if self.opt_local.isChecked():
                args.append("--local-account")
        elif not self.opt_verify.isChecked():
            args.append("--no-verify")

        appimage = os.environ.get("APPIMAGE")
        if os.geteuid() == 0:
            if appimage:
                return "/usr/bin/env", ["APPIMAGE_EXTRACT_AND_RUN=1", appimage] + args
            return sys.executable, ["-s", str(WORKER)] + args

        pkexec = find_exe("pkexec")
        if not pkexec:
            return None, None
        envbin = find_exe("env") or "/usr/bin/env"
        if appimage:
            return pkexec, [envbin, "APPIMAGE_EXTRACT_AND_RUN=1", appimage] + args
        return pkexec, [sys.executable, "-s", str(WORKER)] + args

    def start(self):
        iso = self.iso_edit.text()
        dev = self.dev_combo.currentData()
        if not iso or not dev:
            return
        dinfo = next((d for d in self.devices if d["path"] == dev), None)
        iso_size = os.path.getsize(iso)
        compressed = iso.lower().endswith((".bz2", ".xz", ".gz", ".zip"))
        if dinfo and not compressed and dinfo["size"] < iso_size + (100 * 1000 ** 2 if
                                                                  self.effective_mode() == "windows" else 0):
            QMessageBox.warning(self, APP_NAME,
                                "Der Stick ist zu klein.\nISO: %s\nStick: %s" %
                                (human(iso_size), human(dinfo["size"])))
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Alle Daten werden gelöscht")
        box.setText("<b>Alle Daten auf diesem Gerät werden gelöscht:</b><br><br>%s<br><br>"
                    "Fortfahren?" % self.dev_combo.currentText())
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Ja, Stick löschen und erstellen")
        box.button(QMessageBox.Cancel).setText("Abbrechen")
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() != box.button(QMessageBox.Yes):
            return

        prog, args = self.build_command(iso, dev)
        if prog is None:
            QMessageBox.critical(
                self, APP_NAME,
                "pkexec wurde nicht gefunden.\n\nStarte das Programm im Terminal so:\n\n"
                "sudo %s --cli --iso \"%s\" --device %s" %
                (os.environ.get("APPIMAGE", sys.argv[0]), iso, dev))
            return

        self.log.clear()
        self.append_log("$ %s %s" % (prog, " ".join(args)))
        self.finished_ok = False
        self.last_error = None
        self.cancel_requested = False
        self.bar.setValue(0)
        self.status.setText("Warte auf Passwort-Eingabe …")
        self._buf = b""

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self.on_stdout)
        self.proc.readyReadStandardError.connect(self.on_stderr)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_proc_error)
        self.proc.start(prog, args)
        self.update_buttons()

    def on_proc_error(self, err):
        if err == QProcess.FailedToStart:
            self.append_log("Prozess konnte nicht gestartet werden.")

    def on_stdout(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                self.append_log(text)
                continue
            t = msg.get("t")
            if t == "progress":
                self.bar.setValue(int(msg.get("pct", 0) * 10))
                self.status.setText(msg.get("stage", ""))
            elif t == "log":
                self.append_log(msg.get("msg", ""))
            elif t == "done":
                self.finished_ok = True
                self.append_log(msg.get("msg", ""))
            elif t == "error":
                self.last_error = msg.get("msg", "")
                self.append_log("FEHLER: " + self.last_error)

    def on_stderr(self):
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in data.splitlines():
            if line.strip():
                self.append_log(line)

    def on_finished(self, code, status):
        self.on_stdout()
        proc, self.proc = self.proc, None
        proc.deleteLater()
        self.update_buttons()
        self.refresh_devices(force=True)
        if self.finished_ok:
            self.bar.setValue(1000)
            self.status.setText("Fertig!")
            QMessageBox.information(
                self, "Fertig",
                "<b>Der Stick ist fertig!</b><br><br>Du kannst ihn jetzt abziehen.<br><br>"
                "Booten: Beim Start des Ziel-PCs das Boot-Menü öffnen (meist F12, F11, F8 oder "
                "Esc).<br>Steam Deck: <i>Lautstärke runter</i> gedrückt halten und "
                "<i>Power</i> drücken.")
            return
        if self.last_error:
            self.status.setText("Fehlgeschlagen.")
            QMessageBox.critical(self, "Fehlgeschlagen", self.last_error)
            return
        if code in (126, 127) and not self.cancel_requested:
            self.status.setText("Keine Berechtigung.")
            QMessageBox.warning(
                self, "Keine Berechtigung",
                "Die Passwort-Abfrage wurde abgebrochen oder abgelehnt.<br><br>"
                "<b>Steam Deck / SteamOS:</b> Der Benutzer <i>deck</i> braucht ein Passwort. "
                "Falls du noch keins hast: Konsole öffnen und <code>passwd</code> eingeben, "
                "dann nochmal versuchen.")
            return
        self.status.setText("Abgebrochen." if self.cancel_requested else
                            "Beendet (Code %d)." % code)
        if not self.cancel_requested:
            if not self.log_toggle.isChecked():
                self.log_toggle.setChecked(True)
            QMessageBox.critical(self, "Fehler",
                                 "Der Vorgang wurde unerwartet beendet (Code %d).\n"
                                 "Details siehe Protokoll." % code)

    def cancel(self):
        if self.proc is None:
            return
        if QMessageBox.question(self, "Abbrechen?",
                                "Vorgang abbrechen? Der Stick ist danach unbrauchbar, bis er "
                                "neu erstellt wird.") != QMessageBox.Yes:
            return
        self.cancel_requested = True
        self.status.setText("Breche ab …")
        try:
            self.proc.write(b"cancel\n")
            self.proc.closeWriteChannel()
        except Exception:
            pass

    def closeEvent(self, e):
        if self.proc is not None:
            if QMessageBox.question(self, "Beenden?",
                                    "Der Stick wird gerade erstellt. Wirklich abbrechen und "
                                    "beenden?") != QMessageBox.Yes:
                e.ignore()
                return
            self.cancel_requested = True
            try:
                self.proc.write(b"cancel\n")
                self.proc.closeWriteChannel()
                self.proc.waitForFinished(15000)
            except Exception:
                pass
        e.accept()


def main():
    selftest = "--selftest" in sys.argv
    argv = [a for a in sys.argv if a != "--selftest"]
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("winusb-steamos")
    icon_path = os.environ.get("WINUSB_ICON")
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    app.setStyle("Fusion")
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
        tr = QTranslator(app)
        if tr.load(QLocale.system(), "qtbase", "_",
                   QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(tr)
    except Exception:
        pass
    iso = next((a for a in argv[1:] if not a.startswith("-") and os.path.isfile(a)), None)
    w = MainWindow(iso)
    w.show()
    if selftest:
        def done():
            print("GUI_SELFTEST_OK devices=%d platform=%s" %
                  (len(w.devices), QApplication.platformName()), flush=True)
            app.quit()
        QTimer.singleShot(1500, done)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
