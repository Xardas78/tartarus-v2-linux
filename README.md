# Tartarus V2 Linux Configurator

Linux-Treiber + grafische Konfigurationsoberfläche für die Razer Tartarus V2 (KDE Plasma / PySide6).

## Über dieses Projekt

Razer bietet keine offizielle Linux-Unterstützung für die Tartarus V2 (Synapse ist Windows-only).
Dieses Projekt baut auf dem quelloffenen Kernel-Treiber von
[Drayux/Tartarus](https://github.com/Drayux/Tartarus) auf und ergänzt ihn um:

- ein sauberes Python-Backend (`tartarus_backend.py`) für die sysfs-Schnittstelle des Treibers
- eine native Qt/PySide6-GUI (`tartarus_gui.py`) zur Bearbeitung der 8 Geräteprofile
- eine udev-Regel für Schreibzugriff ohne root

## Komponenten

| Datei | Zweck |
|---|---|
| `tartarus.c`, `module.h`, `keymap.h`, `dkms.conf`, `Makefile` | Kernel-Treiber (Original von Drayux, unverändert) |
| `tartarus_backend.py` | Python-API für Profile lesen/schreiben |
| `tartarus_gui.py` | Grafische Oberfläche |
| `99-tartarus.rules` | udev-Regel (Gruppe `tartarus`) |

## Status

Alle 25 physischen Tasten (1-20, Circle, Steuerkreuz) vollständig getestet und unterstützt.
Mausrad (Scrollen + Mittelklick) funktioniert mit Festverhalten, ist aber noch nicht
profilabhängig konfigurierbar (Treiber-seitige Limitierung, siehe unten).

## Bekannte Einschränkungen

- Mausrad-Profile sind im Treiber nicht implementiert (`profile_show`/`profile_store`
  haben für `MOUSE_INUM` nur ein `// TODO`)
- Bind-Format unterstützt keine Modifier-Kombinationen (z.B. Strg+1) in einem einzelnen Bind
- Interface 1 (EXT) hat im Original-Treiber keine aktive Funktion (Stub)

## Installation

```bash
sudo groupadd -f tartarus
sudo usermod -aG tartarus $USER
sudo cp 99-tartarus.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules

sudo dkms add -m tartarus -v 0.1
sudo dkms build -m tartarus -v 0.1
sudo dkms install -m tartarus -v 0.1
sudo modprobe tartarus

pip install pyside6   # oder: sudo pacman -S pyside6
python3 tartarus_gui.py
```

## Lizenz-Hinweis

Die Kernel-Treiber-Dateien (`tartarus.c`, `module.h`, `keymap.h`, `dkms.conf`, `Makefile`)
stammen vom [Drayux/Tartarus](https://github.com/Drayux/Tartarus)-Projekt und stehen unter
dessen Lizenz (GPL, siehe `MODULE_LICENSE("GPL")` in `module.h`). Die Python-Tools
(`tartarus_backend.py`, `tartarus_gui.py`) sind eigene Arbeit für dieses Projekt — falls
du dafür eine eigene Lizenz festlegen willst (z.B. MIT), füg gerne eine `LICENSE`-Datei
hinzu, bevor das Repo public geht.
