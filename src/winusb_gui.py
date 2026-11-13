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

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget, QToolButton,
)

APP_NAME = "WinUSB-SteamOS"
HERE = Path(__file__).resolve().parent
WORKER = HERE / "winusb_worker.py"
REPO_URL = "https://github.com/elkarbeenus/WinUSB-SteamOS"
sys.path.insert(0, str(HERE))
from winusb_worker import detect_mode  # noqa: E402


def load_build_info():
    info = {"version": "dev", "date": "", "commit": ""}
    try:
        with open(HERE / "build_info.json", encoding="utf-8") as f:
            info.update(json.load(f))
    except (OSError, ValueError):
        pass
    return info


BUILD = load_build_info()
SETTINGS = QSettings("WinUSB-SteamOS", "WinUSB-SteamOS")


def system_lang():
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var, "")
        if v:
            return "de" if v.lower().startswith("de") else "en"
    return "en"


def configured_lang():
    v = str(SETTINGS.value("lang", "auto") or "auto")
    return v if v in ("de", "en") else system_lang()


LANG = configured_lang()


def t(de, en):
    """inline translation: German / English"""
    return de if LANG == "de" else en


def build_date_text():
    d = BUILD.get("date") or ""
    try:
        y, m, dd = d.split("-")
        return "%s.%s.%s" % (dd, m, y) if LANG == "de" else "%s/%s/%s" % (dd, m, y)
    except ValueError:
        return t("unbekannt", "unknown")


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
        kind = t("SD-Karte", "SD card") if name.startswith("mmcblk") else "USB"
        text = "%s  –  %s  (%s)" % (model or kind, human(size), path)
        if labels:
            text += "  [%s]" % ", ".join(labels)
        devs.append({"path": path, "size": size, "text": text})
    return devs


class MainWindow(QMainWindow):
    def __init__(self, iso=None):
        super().__init__()
        self.setWindowTitle(t("%s – bootfähigen USB-Stick erstellen", "%s – create a bootable USB stick") % APP_NAME)
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

        title = QLabel(t("<b style='font-size:15pt'>Bootfähigen USB-Stick erstellen</b><br>"
                         "<span style='color:gray'>Windows 10/11-ISO, SteamOS-Recovery-Image oder "
                         "Linux-ISO – direkt unter SteamOS.</span>",
                         "<b style='font-size:15pt'>Create a bootable USB stick</b><br>"
                         "<span style='color:gray'>Windows 10/11 ISO, SteamOS recovery image or "
                         "Linux ISO – right on SteamOS.</span>"))
        title.setTextFormat(Qt.RichText)
        lay.addWidget(title)

        # --- ISO ---
        g1 = QGroupBox("1. ISO / Image")
        v1 = QVBoxLayout(g1)
        h1 = QHBoxLayout()
        v1.addLayout(h1)
        self.iso_edit = QLineEdit()
        self.iso_edit.setPlaceholderText(t("Datei auswählen oder hierher ziehen (.iso, .img, .img.bz2, .zip …)",
                                          "Choose a file or drop it here (.iso, .img, .img.bz2, .zip …)"))
        self.iso_edit.setReadOnly(True)
        self.iso_btn = QPushButton(t("Auswählen …", "Browse …"))
        self.iso_btn.clicked.connect(self.choose_iso)
        h1.addWidget(self.iso_edit, 1)
        h1.addWidget(self.iso_btn)
        hm = QHBoxLayout()
        hm.addWidget(QLabel(t("Modus:", "Mode:")))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(t("Automatisch erkennen", "Detect automatically"), "auto")
        self.mode_combo.addItem(t("Windows-Installer (ISO → FAT32, wie Rufus)", "Windows installer (ISO → FAT32, like Rufus)"), "windows")
        self.mode_combo.addItem(t("Image 1:1 schreiben (SteamOS, Linux – wie Etcher/dd)", "Write image 1:1 (SteamOS, Linux – like Etcher/dd)"), "raw")
        self.mode_combo.currentIndexChanged.connect(self.update_mode)
        hm.addWidget(self.mode_combo, 1)
        v1.addLayout(hm)
        self.mode_hint = QLabel()
        self.mode_hint.setStyleSheet("color: gray")
        self.mode_hint.setWordWrap(True)
        v1.addWidget(self.mode_hint)
        lay.addWidget(g1)

        # --- device ---
        g2 = QGroupBox(t("2. USB-Stick", "2. USB stick"))
        v2 = QVBoxLayout(g2)
        h2 = QHBoxLayout()
        self.dev_combo = QComboBox()
        self.dev_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_btn = QPushButton(t("Aktualisieren", "Refresh"))
        self.refresh_btn.clicked.connect(lambda: self.refresh_devices(force=True))
        h2.addWidget(self.dev_combo, 1)
        h2.addWidget(self.refresh_btn)
        v2.addLayout(h2)
        self.dev_hint = QLabel()
        self.dev_hint.setStyleSheet("color: gray")
        v2.addWidget(self.dev_hint)
        lay.addWidget(g2)

        # --- options ---
        g3 = QGroupBox(t("3. Optionen", "3. Options"))
        v3 = QVBoxLayout(g3)
        self.g3 = g3
        self.opt_bypass = QCheckBox(t("Windows 11: TPM-, Secure-Boot-, RAM- und CPU-Prüfung umgehen "
                                      "(für ältere PCs)",
                                      "Windows 11: bypass TPM, Secure Boot, RAM and CPU checks "
                                      "(for older PCs)"))
        self.opt_local = QCheckBox(t("Lokales Konto anlegen (kein Microsoft-Konto nötig):",
                                     "Create a local account (no Microsoft account needed):"))
        self.account_edit = QLineEdit(str(SETTINGS.value("account_name", "") or ""))
        self.account_edit.setPlaceholderText(t("Benutzername", "User name"))
        self.account_edit.setMaxLength(20)
        self.account_edit.setEnabled(False)
        self.opt_local.toggled.connect(self.account_edit.setEnabled)
        self.opt_local.setToolTip(t("Wie bei Rufus: Das Konto wird ohne Passwort angelegt, beim ersten "
                                    "Anmelden legst du das Passwort fest.",
                                    "Like Rufus: the account is created without a password, you set "
                                    "the password at first sign-in."))
        self.account_row = QWidget()
        hl = QHBoxLayout(self.account_row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.opt_local)
        hl.addWidget(self.account_edit, 1)
        self.opt_privacy = QCheckBox(t("Datenschutzfragen und Lizenzseite überspringen",
                                       "Skip privacy questions and license page"))
        self.opt_nobitlocker = QCheckBox(t("Automatische BitLocker-Verschlüsselung verhindern",
                                           "Prevent automatic BitLocker device encryption"))
        for w_, key in ((self.opt_bypass, "opt_bypass"), (self.opt_local, "opt_local"),
                        (self.opt_privacy, "opt_privacy"), (self.opt_nobitlocker, "opt_nobitlocker")):
            w_.setChecked(str(SETTINGS.value(key, "false")).lower() == "true")
        v3.addWidget(self.opt_bypass)
        v3.addWidget(self.account_row)
        v3.addWidget(self.opt_privacy)
        v3.addWidget(self.opt_nobitlocker)
        self.opt_verify = QCheckBox(t("Nach dem Schreiben überprüfen (empfohlen)", "Verify after writing (recommended)"))
        self.opt_verify.setChecked(True)
        v3.addWidget(self.opt_verify)
        ha = QHBoxLayout()
        ha.addWidget(QLabel(t("Nach Abschluss:", "When finished:")))
        self.after_combo = QComboBox()
        self.after_combo.addItem(t("Nichts tun", "Do nothing"), "none")
        self.after_combo.addItem(t("Standby (Energie sparen)", "Sleep (suspend)"), "suspend")
        self.after_combo.addItem(t("Gerät herunterfahren", "Shut down this device"), "poweroff")
        ha.addWidget(self.after_combo, 1)
        v3.addLayout(ha)
        note = QLabel(t("Windows-Sticks booten im UEFI-Modus (alle PCs ab ca. 2012 und das Steam Deck).",
                        "Windows sticks boot in UEFI mode (all PCs since about 2012 and the Steam Deck)."))
        note.setStyleSheet("color: gray")
        note.setWordWrap(True)
        v3.addWidget(note)
        lay.addWidget(g3)

        # --- progress ---
        self.status = QLabel(t("Bereit.", "Ready."))
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        lay.addWidget(self.status)
        lay.addWidget(self.bar)

        self.log_toggle = QToolButton()
        self.log_toggle.setText(t("Details anzeigen", "Show details"))
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
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("Deutsch", "de")
        self.lang_combo.addItem("English", "en")
        self.lang_combo.setCurrentIndex(0 if LANG == "de" else 1)
        self.lang_combo.setToolTip("Sprache / Language")
        self.lang_combo.currentIndexChanged.connect(self.change_language)
        self.cancel_btn = QPushButton(t("Abbrechen", "Cancel"))
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setEnabled(False)
        self.start_btn = QPushButton(t("Stick erstellen", "Create stick"))
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 22px;")
        self.start_btn.clicked.connect(self.start)
        hb.addWidget(self.about_btn)
        hb.addWidget(self.lang_combo)
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
        self.log_toggle.setText(t("Details ausblenden", "Hide details") if on else t("Details anzeigen", "Show details"))

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
            self, t("ISO oder Image auswählen", "Choose ISO or image"), start,
            t("ISO und Images", "ISOs and images") +
            " (*.iso *.ISO *.img *.IMG *.raw *.bz2 *.xz *.gz *.zip);;Windows ISO (*.iso *.ISO);;" +
            t("Alle Dateien", "All files") + " (*)")
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
        for w_ in (self.opt_bypass, self.account_row, self.opt_privacy, self.opt_nobitlocker):
            w_.setVisible(win)
        self.opt_verify.setVisible(not win)
        if not p:
            self.mode_hint.setText("")
        elif win:
            self.mode_hint.setText(t("→ Windows-Installations-Stick: FAT32, große install.wim wird "
                                     "automatisch aufgeteilt.",
                                     "→ Windows installation stick: FAT32, a large install.wim is "
                                     "split automatically."))
        else:
            self.mode_hint.setText(t("→ Wird 1:1 auf den Stick geschrieben (z.B. SteamOS-Recovery-Image, "
                                     "Ubuntu, Bazzite). Komprimierte Dateien werden direkt entpackt.",
                                     "→ Written 1:1 to the stick (e.g. SteamOS recovery image, Ubuntu, "
                                     "Bazzite). Compressed files are decompressed on the fly."))

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
            self.dev_hint.setText(t("Achtung: Alle Daten auf dem gewählten Stick werden gelöscht.",
                                     "Warning: all data on the selected stick will be erased."))
        else:
            self.dev_hint.setText(t("Kein USB-Stick gefunden – einfach einstecken, er erscheint "
                                    "automatisch (mind. 8 GB).",
                                    "No USB stick found – just plug one in, it shows up "
                                    "automatically (at least 8 GB)."))
        self.update_buttons()

    def update_buttons(self):
        busy = self.proc is not None
        self.start_btn.setEnabled(not busy and bool(self.iso_edit.text()) and
                                  self.dev_combo.count() > 0)
        self.account_edit.setEnabled(not busy and self.opt_local.isChecked())
        for w in (self.iso_btn, self.dev_combo, self.refresh_btn, self.opt_bypass, self.opt_local,
                  self.opt_privacy, self.opt_nobitlocker,
                  self.opt_verify, self.mode_combo, self.lang_combo):
            w.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)

    def about(self):
        commit = (" (%s)" % BUILD["commit"]) if BUILD.get("commit") else ""
        QMessageBox.about(
            self, "Info",
            "<h3>%s %s</h3>" % (APP_NAME, BUILD.get("version", "dev")) +
            "<p><b>%s</b> %s%s</p>" % (t("Build vom", "Build date"), build_date_text(), commit) +
            "<p><b>%s</b> Dominic Lesina &times; Claude</p>" % t("Erstellt von", "Created by") +
            "<p>" + t("Bootfähige USB-Sticks unter SteamOS und anderen Linux-Systemen – ohne "
                      "etwas zu installieren und ohne Schreibzugriff auf das System.",
                      "Bootable USB sticks on SteamOS and other Linux systems – nothing to "
                      "install, no write access to the system needed.") + "</p>"
            "<p>" + t("Windows: MBR + FAT32, große install.wim wird mit wimlib aufgeteilt (wie Rufus/WoeUSB).<br>"
                      "Images: 1:1 mit Überprüfung (wie Etcher).",
                      "Windows: MBR + FAT32, large install.wim split with wimlib (like Rufus/WoeUSB).<br>"
                      "Images: written 1:1 with verification (like Etcher).") + "</p>"
            "<p><a href='%s'>%s</a><br>%s: MIT</p>" % (REPO_URL, REPO_URL, t("Lizenz", "License")))

    def change_language(self, *_):
        global LANG
        new = self.lang_combo.currentData()
        if new == LANG or self.proc is not None:
            return
        SETTINGS.setValue("lang", new)
        SETTINGS.sync()
        LANG = new
        install_qt_translator(QApplication.instance())
        geo = self.geometry()
        w = MainWindow(self.iso_edit.text() or None)
        w.setGeometry(geo)
        w.mode_combo.setCurrentIndex(self.mode_combo.currentIndex())
        w.opt_bypass.setChecked(self.opt_bypass.isChecked())
        w.opt_local.setChecked(self.opt_local.isChecked())
        w.account_edit.setText(self.account_edit.text())
        w.opt_privacy.setChecked(self.opt_privacy.isChecked())
        w.opt_nobitlocker.setChecked(self.opt_nobitlocker.isChecked())
        w.opt_verify.setChecked(self.opt_verify.isChecked())
        w.after_combo.setCurrentIndex(self.after_combo.currentIndex())
        idx = w.dev_combo.findData(self.dev_combo.currentData())
        if idx >= 0:
            w.dev_combo.setCurrentIndex(idx)
        set_main(w)
        w.show()
        self.timer.stop()
        self.close()
        self.deleteLater()

    def show_done(self):
        QMessageBox.information(
            self, t("Fertig", "Done"),
            t("<b>Der Stick ist fertig!</b><br><br>Du kannst ihn jetzt abziehen.<br><br>"
              "Booten: Beim Start des Ziel-PCs das Boot-Menü öffnen (meist F12, F11, F8 oder "
              "Esc).<br>Steam Deck: <i>Lautstärke runter</i> gedrückt halten und "
              "<i>Power</i> drücken.",
              "<b>The stick is ready!</b><br><br>You can remove it now.<br><br>"
              "To boot: open the boot menu when the target PC starts (usually F12, F11, F8 or "
              "Esc).<br>Steam Deck: hold <i>Volume down</i> and press <i>Power</i>."))

    def countdown_after(self, action):
        """Ask with a 30 s countdown, then suspend / power off. Returns True if handled."""
        secs = [30]
        what = t("Standby", "Sleep") if action == "suspend" else t("Herunterfahren", "Shut down")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(t("Fertig", "Done"))
        go = box.addButton(t("Jetzt", "Now"), QMessageBox.AcceptRole)
        stop = box.addButton(t("Abbrechen", "Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(go)

        def text():
            box.setText(t("<b>Der Stick ist fertig!</b><br><br>%s in %d Sekunden …",
                          "<b>The stick is ready!</b><br><br>%s in %d seconds …") % (what, secs[0]))
        text()
        timer = QTimer(box)

        def tick():
            secs[0] -= 1
            if secs[0] <= 0:
                timer.stop()
                box.done(0)
            else:
                text()
        timer.timeout.connect(tick)
        timer.start(1000)
        box.exec()
        timer.stop()
        if box.clickedButton() == stop:
            self.show_done()
            return True
        cmd = ["systemctl", "suspend"] if action == "suspend" else ["systemctl", "poweroff"]
        exe = find_exe("systemctl") or "systemctl"
        ok = QProcess.startDetached(exe, cmd[1:])
        if isinstance(ok, tuple):
            ok = ok[0]
        if not ok:
            QMessageBox.warning(self, APP_NAME, t("%s konnte nicht ausgelöst werden.",
                                                  "Could not trigger %s.") % what)
        if action == "suspend":
            self.show_done()
        return True

    # ------------------------------------------------------------ run
    def build_command(self, iso, dev):
        mode = self.effective_mode()
        args = ["--worker", "--iso", iso, "--device", dev, "--mode", mode, "--lang", LANG]
        if mode == "windows":
            if self.opt_bypass.isChecked():
                args.append("--win11-bypass")
            if self.opt_local.isChecked() and self.account_edit.text().strip():
                args += ["--local-account", "--user", self.account_edit.text().strip()]
            if self.opt_privacy.isChecked():
                args.append("--skip-privacy")
            if self.opt_nobitlocker.isChecked():
                args.append("--no-bitlocker")
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
        if self.effective_mode() == "windows" and self.opt_local.isChecked():
            name = self.account_edit.text().strip()
            if not name or any(c in name for c in '/\\[]:;|=,+*?<>"@%') or \
                    name.lower() in ("administrator", "admin", "guest", "defaultaccount", "system",
                                     "user", "users", "wdagutilityaccount"):
                QMessageBox.warning(self, APP_NAME,
                                    t("Bitte einen gültigen Benutzernamen für das lokale Konto eingeben "
                                      "(keine Sonderzeichen wie / \\ : * ? \" < > | @, nicht "
                                      "\"Administrator\" oder \"Gast\").",
                                      "Please enter a valid user name for the local account (no special "
                                      "characters like / \\ : * ? \" < > | @, not \"Administrator\" "
                                      "or \"Guest\")."))
                self.account_edit.setFocus()
                return
        for w_, key in ((self.opt_bypass, "opt_bypass"), (self.opt_local, "opt_local"),
                        (self.opt_privacy, "opt_privacy"), (self.opt_nobitlocker, "opt_nobitlocker")):
            SETTINGS.setValue(key, "true" if w_.isChecked() else "false")
        SETTINGS.setValue("account_name", self.account_edit.text().strip())
        dinfo = next((d for d in self.devices if d["path"] == dev), None)
        iso_size = os.path.getsize(iso)
        compressed = iso.lower().endswith((".bz2", ".xz", ".gz", ".zip"))
        if dinfo and not compressed and dinfo["size"] < iso_size + (100 * 1000 ** 2 if
                                                                  self.effective_mode() == "windows" else 0):
            QMessageBox.warning(self, APP_NAME,
                                t("Der Stick ist zu klein.\nISO: %s\nStick: %s",
                                  "The stick is too small.\nISO: %s\nStick: %s") %
                                (human(iso_size), human(dinfo["size"])))
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(t("Alle Daten werden gelöscht", "All data will be erased"))
        box.setText(t("<b>Alle Daten auf diesem Gerät werden gelöscht:</b><br><br>%s<br><br>Fortfahren?",
                      "<b>All data on this device will be erased:</b><br><br>%s<br><br>Continue?")
                    % self.dev_combo.currentText())
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText(t("Ja, Stick löschen und erstellen", "Yes, erase and create"))
        box.button(QMessageBox.Cancel).setText(t("Abbrechen", "Cancel"))
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() != box.button(QMessageBox.Yes):
            return

        prog, args = self.build_command(iso, dev)
        if prog is None:
            QMessageBox.critical(
                self, APP_NAME,
                t("pkexec wurde nicht gefunden.\n\nStarte das Programm im Terminal so:\n\n",
                  "pkexec was not found.\n\nRun the program in a terminal like this:\n\n") +
                "sudo %s --cli --iso \"%s\" --device %s" %
                (os.environ.get("APPIMAGE", sys.argv[0]), iso, dev))
            return

        self.log.clear()
        self.append_log("$ %s %s" % (prog, " ".join(args)))
        self.finished_ok = False
        self.last_error = None
        self.cancel_requested = False
        self.bar.setValue(0)
        self.status.setText(t("Warte auf Passwort-Eingabe …", "Waiting for password …"))
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
            self.append_log(t("Prozess konnte nicht gestartet werden.", "Process could not be started."))

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
            kind = msg.get("t")
            if kind == "progress":
                self.bar.setValue(int(msg.get("pct", 0) * 10))
                self.status.setText(msg.get("stage", ""))
            elif kind == "log":
                self.append_log(msg.get("msg", ""))
            elif kind == "done":
                self.finished_ok = True
                self.append_log(msg.get("msg", ""))
            elif kind == "error":
                self.last_error = msg.get("msg", "")
                self.append_log(t("FEHLER: ", "ERROR: ") + self.last_error)

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
            self.status.setText(t("Fertig!", "Done!"))
            action = self.after_combo.currentData()
            if action in ("suspend", "poweroff") and self.countdown_after(action):
                return
            self.show_done()
            return
        if self.last_error:
            self.status.setText(t("Fehlgeschlagen.", "Failed."))
            QMessageBox.critical(self, t("Fehlgeschlagen", "Failed"), self.last_error)
            return
        if code in (126, 127) and not self.cancel_requested:
            self.status.setText(t("Keine Berechtigung.", "Permission denied."))
            QMessageBox.warning(
                self, t("Keine Berechtigung", "Permission denied"),
                t("Die Passwort-Abfrage wurde abgebrochen oder abgelehnt.<br><br>"
                  "<b>Steam Deck / SteamOS:</b> Der Benutzer <i>deck</i> braucht ein Passwort. "
                  "Falls du noch keins hast: Konsole öffnen und <code>passwd</code> eingeben, "
                  "dann nochmal versuchen.",
                  "The password prompt was cancelled or denied.<br><br>"
                  "<b>Steam Deck / SteamOS:</b> the user <i>deck</i> needs a password. "
                  "If you don't have one yet: open Konsole, run <code>passwd</code>, "
                  "then try again."))
            return
        self.status.setText(t("Abgebrochen.", "Cancelled.") if self.cancel_requested else
                            t("Beendet (Code %d).", "Exited (code %d).") % code)
        if not self.cancel_requested:
            if not self.log_toggle.isChecked():
                self.log_toggle.setChecked(True)
            QMessageBox.critical(self, t("Fehler", "Error"),
                                 t("Der Vorgang wurde unerwartet beendet (Code %d).\n"
                                   "Details siehe Protokoll.",
                                   "The process ended unexpectedly (code %d).\n"
                                   "See the details log.") % code)

    def cancel(self):
        if self.proc is None:
            return
        if QMessageBox.question(self, t("Abbrechen?", "Cancel?"),
                                t("Vorgang abbrechen? Der Stick ist danach unbrauchbar, bis er "
                                  "neu erstellt wird.",
                                  "Cancel? The stick will be unusable until it is created "
                                  "again.")) != QMessageBox.Yes:
            return
        self.cancel_requested = True
        self.status.setText(t("Breche ab …", "Cancelling …"))
        try:
            self.proc.write(b"cancel\n")
            self.proc.closeWriteChannel()
        except Exception:
            pass

    def closeEvent(self, e):
        if self.proc is not None:
            if QMessageBox.question(self, t("Beenden?", "Quit?"),
                                    t("Der Stick wird gerade erstellt. Wirklich abbrechen und "
                                      "beenden?",
                                      "The stick is being created. Really cancel and "
                                      "quit?")) != QMessageBox.Yes:
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


MAIN = [None]


def set_main(w):
    MAIN[0] = w


def install_qt_translator(app):
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    old = getattr(app, "_qt_tr", None)
    if old is not None:
        app.removeTranslator(old)
        app._qt_tr = None
    if LANG != "de":
        return
    try:
        tr = QTranslator(app)
        if tr.load(QLocale("de_DE"), "qtbase", "_",
                   QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(tr)
            app._qt_tr = tr
    except Exception:
        pass


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
    install_qt_translator(app)
    iso = next((a for a in argv[1:] if not a.startswith("-") and os.path.isfile(a)), None)
    w = MainWindow(iso)
    set_main(w)
    w.show()
    if selftest:
        def switch():
            # exercise the language switch once
            MAIN[0].lang_combo.setCurrentIndex(1 - MAIN[0].lang_combo.currentIndex())

        def done():
            mw = MAIN[0]
            print("GUI_SELFTEST_OK devices=%d platform=%s lang=%s version=%s date=%s" %
                  (len(mw.devices), QApplication.platformName(), LANG, BUILD.get("version"),
                   build_date_text()), flush=True)
            app.quit()
        QTimer.singleShot(700, switch)
        QTimer.singleShot(1500, done)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
