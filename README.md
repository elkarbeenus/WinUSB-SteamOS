# WinUSB-SteamOS

Bootfähigen **Windows-10/11-Installations-Stick aus einer ISO** erstellen, direkt unter **SteamOS** (Desktop-Modus), auf dem Steam Deck und unter anderen Linux-Systemen.
Ein einziges AppImage: nichts installieren, kein `steamos-readonly disable`, kein pacman.

![build](https://github.com/elkarbeenus/WinUSB-SteamOS/actions/workflows/build.yml/badge.svg)

## Warum nicht WoeUSB-ng?

WoeUSB-ng (auch als AppImage) will unter `/media/…` einhängen. Auf SteamOS ist das Root-Dateisystem schreibgeschützt, deshalb kommt
`Einhängepunkt-Verzeichnis /media/woeusb_source_… konnte nicht erstellt werden`.

WinUSB-SteamOS hängt **nur in einem temporären Ordner unter `/tmp`** ein und schreibt nirgends ins System.

| | WinUSB-SteamOS | WoeUSB-ng AppImage |
|---|---|---|
| Läuft auf schreibgeschütztem SteamOS | ✅ | ❌ (`/media`) |
| Benötigte Systempakete | keine (wimlib + mkfs.fat sind dabei) | wimlib, parted, grub … |
| Windows-11-Bypass (TPM/Secure Boot/RAM) | ✅ wie Rufus | ❌ |
| install.wim > 4 GB | wird in `install.swm`-Teile aufgeteilt | ✅ |

## Benutzung

1. Bei [**Releases**](https://github.com/elkarbeenus/WinUSB-SteamOS/releases/latest) die Datei `WinUSB-SteamOS-x86_64.AppImage` herunterladen.
2. Rechtsklick → *Eigenschaften* → *Berechtigungen* → **Ausführbar** anhaken.
3. Doppelklick → ISO wählen (oder aufs Fenster ziehen) → USB-Stick wählen → **Stick erstellen**.
4. Passwort eingeben (die Abfrage kommt vom System, nötig für den Zugriff auf den Stick).

> **Steam Deck:** Der Benutzer `deck` hat ab Werk **kein Passwort**. Einmal in der Konsole `passwd` eingeben und eins festlegen, sonst kann die Passwortabfrage nicht bestätigt werden.

**Booten:** Am Ziel-PC das Boot-Menü öffnen (meist F12 / F11 / F8 / Esc).
Steam Deck: **Lautstärke runter** gedrückt halten und **Power** drücken.

### Text-Modus (Terminal)

```bash
sudo ./WinUSB-SteamOS-x86_64.AppImage --cli --iso ~/Downloads/Win11.iso --device /dev/sdX \
     [--win11-bypass] [--local-account]
```

## Wie es funktioniert

Angelehnt an Rufus (FAT32-Modus) und WoeUSB-ng:

1. ISO wird schreibgeschützt per Loop-Device unter `/tmp/winusb-XXXX/iso` eingehängt (UDF).
2. Alte Partitionstabellen (auch GPT-Backup am Ende) werden gelöscht. Es wird eine **MBR**-Tabelle mit **einer FAT32-Partition** (Typ `0x0C`, aktiv) geschrieben, direkt im Programm, ohne parted oder sfdisk.
3. `mkfs.fat -F 32` (gebündelt).
4. Alle Dateien werden kopiert, regelmäßig mit `fdatasync`, damit der Fortschrittsbalken stimmt und am Ende kein minutenlanges „sync“ kommt.
5. `install.wim` > 4 GB wird mit **wimlib** in `install.swm`, `install2.swm` … aufgeteilt. Das unterstützt Windows Setup nativ.
6. Optional: `autounattend.xml` mit den Rufus-Tweaks (LabConfig-Bypass, BypassNRO).

Der Stick bootet per **UEFI**: alle PCs ab ca. 2012 und das Steam Deck. Windows 11 setzt UEFI sowieso voraus.

### Rechte und AppImage

Die Oberfläche läuft als normaler Benutzer. Für das Schreiben auf den Stick startet sie sich selbst über
`pkexec env APPIMAGE_EXTRACT_AND_RUN=1 <AppImage> --worker …`: root kann nicht in den FUSE-Mount eines anderen Benutzers schauen. Das ist ein typischer Grund, warum andere AppImages mit pkexec scheitern.
Zum Abbrechen wird dem Root-Prozess einfach die Eingabe geschlossen.

Sicherheitsprüfungen: nur ganze USB-/Wechseldatenträger, keine Geräte mit eingehängtem System (`/`, `/home`, `/var` …), keine LVM/RAID/LUKS-Geräte.

## Selbst bauen

```bash
# Ubuntu 22.04 (oder Container), braucht uv, patchelf, build-essential
./build.sh
# -> dist/WinUSB-SteamOS-x86_64.AppImage
```

Inhalt: portables Python (python-build-standalone), PySide6 (Qt 6), statisch gebautes `mkfs.fat`, `wimlib-imagex`, AppImage-Runtime (static, braucht kein libfuse2).

## CI-Tests

Jeder Push baut das AppImage und testet es (siehe `tests/ci_test.sh`):

- echte UDF-ISO wie bei Microsoft, mit `install.wim` > 4 GB
- 8-GB-„Stick“ (Loop-Device) mit altem GPT + ext4 darauf
- Ausführung in einem **Arch-Linux-Container** (Basis von SteamOS), **ohne FUSE**, genau wie über pkexec
- Prüfung: Partitionstabelle, `fsck.fat`, Prüfsummen, `wimlib-imagex verify` der Teile, autounattend.xml
- Sicherheitsprüfungen (Partition statt Gerät, internes Laufwerk, Abbruch)
- GUI-Start auf einem virtuellen X-Server unter Arch

## Lizenz

MIT. Gebündelte Programme: wimlib (LGPLv3+/GPLv3+), dosfstools (GPLv3+), Qt/PySide6 (LGPLv3), Python (PSF).
