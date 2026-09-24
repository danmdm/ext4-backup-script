#!/usr/bin/env python3
"""
Backup inteligent Linux -> ext4 cu sincronizare rsync și detecție mutări.

Structură pe stick:
    /stick/<hostname>/<cale_absoluta_fara_leading_slash>/...    <- backup curent
    /stick/<hostname>/_Istoric/home/<user>/<timestamp>/...      <- istoric per-user

Prefixul <hostname> izolează backup-urile de pe laptopuri diferite.
Fiecare user are istoricul lui, curățat independent.

Variantă pentru ext4 (fără compatibilitate exFAT/Windows).
"""

import os
import sys
import time
import socket
import shutil
import subprocess
import hashlib
import re
import argparse
import fnmatch
import getpass
import fcntl
from datetime import datetime

# ==========================================
# CONSTANTE
# ==========================================
MARJA_SIGURANTA_MB = 200
ZILE_PASTRARE_ISTORIC = 30
MAX_DELETE_PERCENT_DEFAULT = 50
TIMEOUT_RSYNC_SEC = 3600

BLOC_HASH_RAPID = 65536
LOCK_DIR = "/tmp"

FORMAT_TIMESTAMP_SESIUNE = "%Y-%m-%d_%H-%M-%S"

EXCLUDERI_FISIERE = [
    "*.tmp", "*~", ".~lock.*", "*.part", "*.crdownload",
    "thumbs.db", ".ds_store", "desktop.ini",
]
EXCLUDERI_DIRECTOARE = [
    "__pycache__", ".pytest_cache", ".thumbnails",
    ".trash-*", "$recycle.bin", "system volume information",
    "_sters", "_modif", "_istoric",
]

# ==========================================
# CULORI ȘI EMOJI
# ==========================================
CULORI = {
    "rosu":     "\033[91m",
    "verde":    "\033[92m",
    "galben":   "\033[93m",
    "albastru": "\033[94m",
    "magenta":  "\033[95m",
    "cyan":     "\033[96m",
    "alb":      "\033[97m",
    "gri":      "\033[90m",
    "bold":     "\033[1m",
    "reset":    "\033[0m",
}

EMOJI = {
    "folder":     "📁",
    "index":      "🔍",
    "sync":       "🔄",
    "mutare":     "↔️ ",
    "stergere":   "🗑️ ",
    "succes":     "✅",
    "eroare":     "❌",
    "avert":      "⚠️ ",
    "info":       "ℹ️ ",
    "istoric":    "📜",
    "spatiu":     "💾",
    "timp":       "⏱️ ",
    "sumar":      "📊",
    "locatie":    "📍",
    "laptop":     "💻",
    "scut":       "🛡️ ",
    "lock":       "🔒",
}

ANSI_REGEX = re.compile(r'\033\[[0-9;]*m')


def strip_ansi(s):
    return ANSI_REGEX.sub('', s)


def lungime_vizuala(s):
    s = strip_ansi(s)
    lungime = 0
    for c in s:
        cp = ord(c)
        if cp >= 0x1F300 or cp == 0xFE0F or cp == 0x20E3:
            lungime += 2
        elif 0x2600 <= cp <= 0x27BF:
            lungime += 2
        else:
            lungime += 1
    return lungime


def colorat(text, culoare):
    if not sys.stdout.isatty():
        return text
    return f"{CULORI.get(culoare, '')}{text}{CULORI['reset']}"


def banner(titlu, subtitlu=None, latime=54, culoare="albastru"):
    sus = "╔" + "═" * (latime - 2) + "╗"
    jos = "╚" + "═" * (latime - 2) + "╝"

    def centreaza(text):
        lung = lungime_vizuala(text)
        padding = (latime - 2 - lung) // 2
        rest = latime - 2 - lung - padding
        return "║" + " " * padding + text + " " * rest + "║"

    linii = [sus, centreaza(titlu)]
    if subtitlu:
        linii.append(centreaza(subtitlu))
    linii.append(jos)

    return "\n".join(colorat(l, culoare) for l in linii)


def linie_separator(latime=54, culoare="gri"):
    return colorat("━" * latime, culoare)


def format_durata(secunde):
    if secunde < 60:
        return f"{secunde:.1f} secunde"
    minute = int(secunde // 60)
    sec = int(secunde % 60)
    return f"{minute}m {sec}s"


# ==========================================
# PARSARE ARGUMENTE
# ==========================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Backup inteligent Linux -> ext4 cu rsync și detecție mutări.",
        epilog=(
            "Exemple:\n"
            "  %(prog)s ~/Desktop ~/Documents /media/dan/stick\n"
            "  %(prog)s ~/Desktop /media/dan/stick --dry-run\n"
            "  %(prog)s ~/Desktop /media/dan/stick --verbose\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'cai', nargs='*',
        help='Căile sursă urmate de calea destinație (ultimul = destinația)'
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Simulare fără modificări fizice'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Output detaliat rsync (listing complet per fișier)'
    )
    parser.add_argument(
        '--allow-internal', action='store_true',
        help='Permite backup pe discul intern (nu doar pe filesystem extern)'
    )
    parser.add_argument(
        '--max-delete-percent', type=int, default=MAX_DELETE_PERCENT_DEFAULT,
        help=f'Refuză dacă peste N%% din fișierele de pe stick ar fi șterse '
             f'(default: {MAX_DELETE_PERCENT_DEFAULT}, folosește 100 pentru a dezactiva complet)'
    )
    parser.add_argument(
        '--marja-mb', type=int, default=MARJA_SIGURANTA_MB,
        help=f'Marjă de siguranță în MB (default: {MARJA_SIGURANTA_MB})'
    )
    parser.add_argument(
        '--zile-istoric', type=int, default=ZILE_PASTRARE_ISTORIC,
        help=f'Zile de păstrare istoric (default: {ZILE_PASTRARE_ISTORIC})'
    )
    return parser.parse_args()


# ==========================================
# UTILITARE
# ==========================================
def pauza_finala():
    if sys.stdin.isatty():
        try:
            input("Apasă Enter pentru a închide...")
        except EOFError:
            pass


def trimite_notificare(titlu, mesaj, iconita="dialog-information"):
    if not shutil.which("notify-send"):
        return
    try:
        env = os.environ.copy()
        uid = os.getuid()
        if "XDG_RUNTIME_DIR" not in env:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        subprocess.run(
            ["notify-send", "-i", iconita, titlu, mesaj],
            env=env, check=False,
        )
    except Exception:
        pass


def glob_ci(p):
    return ''.join(
        f'[{c.lower()}{c.upper()}]' if c.isalpha() else c
        for c in p
    )


def e_exclus_fisier(nume):
    nume_lower = nume.lower()
    return any(
        fnmatch.fnmatch(nume_lower, p.lower()) for p in EXCLUDERI_FISIERE
    )


def e_exclus_folder(nume):
    nume_lower = nume.lower()
    return any(
        fnmatch.fnmatch(nume_lower, p.lower()) for p in EXCLUDERI_DIRECTOARE
    )


def walk_filtrat(radacina):
    for root, dirs, files in os.walk(radacina):
        dirs[:] = [d for d in dirs if not e_exclus_folder(d)]
        fisiere_ok = [f for f in files if not e_exclus_fisier(f)]
        yield root, dirs, fisiere_ok


def explica_eroare_rsync(cod):
    explicatii = {
        1:  "Eroare de sintaxă sau utilizare",
        2:  "Eroare de protocol",
        3:  "Eroare la selectarea fișierelor de intrare",
        4:  "Acțiune nesuportată",
        5:  "Eroare la pornirea clientului",
        6:  "Eroare la încărcarea jurnalului",
        10: "Eroare socket I/O",
        11: "Eroare I/O fișier (spațiu, permisiuni, disc deconectat)",
        12: "Eroare protocol de date",
        13: "Eroare la diagnosticare",
        14: "Eroare la IPC",
        20: "Semnal primit (SIGUSR1/SIGINT)",
        21: "Așteptare pentru pid",
        22: "Eroare la alocarea buffer-ului",
        23: "Transfer parțial din cauza unor erori (permisiuni, nume invalide, scriere)",
        24: "Fișiere sursă dispărute în timpul transferului (probabil modificate activ)",
        25: "Limită --max-delete atinsă",
        30: "Timeout de date",
        35: "Timeout de conexiune",
    }
    return explicatii.get(cod, "Cod necunoscut")


# ==========================================
# HASH
# ==========================================
def calculeaza_hash_rapid(cale_fisier):
    try:
        marime = os.path.getsize(cale_fisier)
        if marime == 0:
            return ("empty_file", 0)

        hasher = hashlib.md5()
        hasher.update(str(marime).encode('utf-8'))

        with open(cale_fisier, 'rb') as f:
            hasher.update(f.read(BLOC_HASH_RAPID))

            if marime > 2 * BLOC_HASH_RAPID:
                mijloc = marime // 2 - BLOC_HASH_RAPID // 2
                f.seek(mijloc)
                hasher.update(f.read(BLOC_HASH_RAPID))

            if marime > BLOC_HASH_RAPID:
                f.seek(-BLOC_HASH_RAPID, os.SEEK_END)
                hasher.update(f.read(BLOC_HASH_RAPID))

        return (hasher.hexdigest(), marime)
    except OSError:
        return None


def calculeaza_hash_complet(cale_fisier):
    try:
        marime = os.path.getsize(cale_fisier)
        if marime == 0:
            return ("empty_file", 0)

        hasher = hashlib.md5()
        hasher.update(str(marime).encode('utf-8'))

        with open(cale_fisier, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                hasher.update(chunk)

        return (hasher.hexdigest(), marime)
    except OSError:
        return None


# ==========================================
# VERIFICĂRI PRELIMINARE
# ==========================================
def verifica_mediu():
    if os.geteuid() == 0:
        print(colorat(f"{EMOJI['eroare']} EROARE: Nu rula scriptul ca root/sudo! Rulează ca user normal.", "rosu"))
        sys.exit(1)

    if not shutil.which("rsync"):
        print(colorat(f"{EMOJI['eroare']} EROARE: `rsync` nu este instalat sau nu e în PATH.", "rosu"))
        print(colorat("   Instalează: sudo apt install rsync", "galben"))
        sys.exit(1)


def verifica_path_special(cale):
    if '\n' in cale:
        return "conține newline (\\n)"
    if '\r' in cale:
        return "conține carriage return (\\r)"
    if '\0' in cale:
        return "conține null byte (\\0)"
    return None


def verifica_filesystem_separat(destinatie, foldere_sursa):
    try:
        dev_dest = os.stat(destinatie).st_dev
    except OSError:
        return False, "nu pot citi destinația"

    try:
        dev_root = os.stat('/').st_dev
        if dev_dest == dev_root:
            return False, "pe același filesystem cu / (discul intern)"
    except OSError:
        pass

    for sursa in foldere_sursa:
        try:
            if os.stat(sursa).st_dev == dev_dest:
                return False, f"pe același filesystem cu sursa {sursa}"
        except OSError:
            pass

    return True, None


def verifica_destinatie(destinatie, foldere_sursa, is_dry_run, allow_internal=False):
    if not os.path.exists(destinatie):
        msg = f"HDD-ul extern nu este conectat la: {destinatie}"
        print(banner("BACKUP EȘUAT", msg, latime=70, culoare="rosu"))
        trimite_notificare("Backup Eșuat", msg, iconita="dialog-error")
        pauza_finala()
        sys.exit(1)

    if not os.path.isdir(destinatie):
        msg = f"Destinația nu este un director: {destinatie}"
        print(banner("BACKUP EȘUAT", msg, latime=70, culoare="rosu"))
        trimite_notificare("Backup Eșuat", msg, iconita="dialog-error")
        pauza_finala()
        sys.exit(1)

    if not allow_internal:
        ok, motiv = verifica_filesystem_separat(destinatie, foldere_sursa)
        if not ok:
            print()
            print(banner("BACKUP OPRIT", None, latime=70, culoare="rosu"))
            print(colorat("Destinația pare a fi pe discul intern, nu pe stick!", "rosu"))
            print()
            print(colorat("Posibile cauze:", "cyan"))
            print("  - Stick-ul nu e montat (mountpoint există, dar e gol)")
            print("  - Cale greșită (ai dat o cale de pe discul intern)")
            print()
            print(f"  Destinație:  {colorat(destinatie, 'cyan')}")
            print(f"  Problemă:    {colorat(motiv, 'magenta')}")
            print()
            print(colorat("Verifică că stick-ul e montat:", "cyan"))
            print(f"  mount | grep \"{destinatie}\"")
            print()
            print(colorat("Dacă chiar vrei să scrii pe discul intern, "
                          "rulează cu --allow-internal.", "cyan"))
            print(colorat("Nicio modificare nu a fost aplicată.", "galben"))
            trimite_notificare(
                "Backup Oprit",
                f"Destinația pare a fi pe discul intern ({motiv})",
                iconita="dialog-error",
            )
            pauza_finala()
            sys.exit(1)

    if not is_dry_run:
        cale_test = os.path.join(destinatie, ".test_scriere.tmp")
        try:
            with open(cale_test, "w") as f:
                f.write("test")
            os.remove(cale_test)
        except OSError:
            msg = "HDD-ul extern este montat Read-Only!"
            print(banner("BACKUP EȘUAT", msg, latime=70, culoare="rosu"))
            print(colorat("Recomandare: verifică permisiunile de scriere pe stick.", "galben"))
            trimite_notificare("Backup Eșuat", msg, iconita="dialog-error")
            pauza_finala()
            sys.exit(1)


def verifica_surse_si_destinatie(foldere_sursa, destinatia_baza):
    toate_caile = list(foldere_sursa) + [destinatia_baza]
    for cale in toate_caile:
        motiv = verifica_path_special(cale)
        if motiv:
            print(banner("EROARE PATH INVALID", None, latime=70, culoare="rosu"))
            print(colorat(f"  Path problematic: {repr(cale)}", "rosu"))
            print(colorat(f"  Problemă:         {motiv}", "rosu"))
            pauza_finala()
            sys.exit(1)

    for i, s1 in enumerate(foldere_sursa):
        for s2 in foldere_sursa[i + 1:]:
            try:
                if (os.path.commonpath([s1, s2]) == s1
                        or os.path.commonpath([s1, s2]) == s2):
                    print(banner("EROARE SURSE SUPRAPUSE", None, latime=70, culoare="rosu"))
                    print(colorat(f"  - {s1}", "rosu"))
                    print(colorat(f"  - {s2}", "rosu"))
                    pauza_finala()
                    sys.exit(1)
            except ValueError:
                pass

    for sursa in foldere_sursa:
        try:
            comun = os.path.commonpath([sursa, destinatia_baza])
            if comun == destinatia_baza or comun == sursa:
                print(banner("EROARE SUPRAPUNERE", None, latime=70, culoare="rosu"))
                if comun == destinatia_baza:
                    print(colorat("  Sursa este în interiorul destinației:", "rosu"))
                    print(colorat(f"    sursă:      {sursa}", "rosu"))
                    print(colorat(f"    destinație: {destinatia_baza}", "rosu"))
                else:
                    print(colorat("  Destinația este în interiorul sursei:", "rosu"))
                    print(colorat(f"    sursă:      {sursa}", "rosu"))
                    print(colorat(f"    destinație: {destinatia_baza}", "rosu"))
                    print()
                    print(colorat("  Acest lucru ar cauza copiere recursivă (backup-ul se", "galben"))
                    print(colorat("  copiază pe el însuși) și ar umple discul!", "galben"))
                pauza_finala()
                sys.exit(1)
        except ValueError:
            pass


# ==========================================
# LOCK (prevenire rulări simultane)
# ==========================================
def get_lock_path(destinatie_baza):
    try:
        st = os.stat(destinatie_baza)
        cheie = f"dev{st.st_dev}"
    except OSError:
        cheie = hashlib.md5(destinatie_baza.encode()).hexdigest()[:16]
    return os.path.join(LOCK_DIR, f"backup_stick_{cheie}.lock")


def achizitioneaza_lock(destinatie_baza):
    lock_path = get_lock_path(destinatie_baza)

    fd = None
    mod = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o666)
        mod = "rw"
        try:
            os.chmod(lock_path, 0o666)
        except OSError:
            pass
    except PermissionError:
        try:
            fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
            mod = "r"
        except OSError as e:
            return None, f"nu pot deschide {lock_path}: {e}"
    except OSError as e:
        return None, f"nu pot deschide {lock_path}: {e}"

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            continut = os.read(fd, 256).decode('utf-8', errors='replace').strip()
        except OSError:
            continut = "(info indisponibil)"
        try:
            os.close(fd)
        except OSError:
            pass
        return None, continut or "(info indisponibil)"

    if mod == "rw":
        # Scriem info înainte de a trunchia, pentru a evita fereastra în care
        # alt proces ar citi un fișier gol (race condition cu ftruncate).
        try:
            info = f"PID={os.getpid()} USER={getpass.getuser()} START={datetime.now().isoformat()}\n"
            info_bytes = info.encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, info_bytes)
            os.ftruncate(fd, len(info_bytes))
            os.fsync(fd)
        except OSError:
            pass

    return fd, None


def elibereaza_lock(lock_fd):
    if lock_fd is None:
        return
    try:
        os.ftruncate(lock_fd, 0)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


# ==========================================
# VERIFICARE SIGURANȚĂ ȘTERGERI
# ==========================================
def verifica_siguranta_stergeri(
    fisiere_sursa_exacte, fisiere_sursa_dupa_hash,
    destinatia_folder, max_delete_percent,
):
    if not os.path.exists(destinatia_folder):
        return

    stick_files = set()
    for root, _, files in walk_filtrat(destinatia_folder):
        for f in files:
            cale_rel = os.path.relpath(
                os.path.join(root, f), destinatia_folder
            )
            stick_files.add(cale_rel)

    if not stick_files:
        return

    stergeri = 0
    for rel in stick_files:
        if rel in fisiere_sursa_exacte:
            continue
        cale_abs = os.path.join(destinatia_folder, rel)
        h = calculeaza_hash_rapid(cale_abs)
        if h and h[0] != "empty_file" and h in fisiere_sursa_dupa_hash:
            continue
        stergeri += 1

    total = len(stick_files)
    procent = stergeri / total * 100 if total else 0

    if not fisiere_sursa_exacte and max_delete_percent < 100:
        print()
        print(banner("BACKUP OPRIT", None, latime=70, culoare="rosu"))
        print(colorat("Sursa pare goală, dar pe stick există fișiere.", "rosu"))
        print()
        print(colorat("Posibile cauze:", "cyan"))
        print("  - Mount eșuat (HDD extern, partajare rețea)")
        print("  - Folder criptat neînchis (LUKS, gocryptfs, Veracrypt)")
        print("  - Folder golit accidental")
        print()
        print(f"  Sursa:              {colorat('0 fișiere', 'rosu')}")
        print(f"  Stick:              {colorat(f'{total} fișiere', 'verde')}")
        print(f"  Ar fi mutate în _STERS: {colorat(str(total), 'magenta')}")
        print()
        print(colorat("Dacă sursa e cu adevărat goală, rulează cu --max-delete-percent 100.", "cyan"))
        print(colorat("Nicio modificare nu a fost aplicată pe HDD.", "galben"))
        trimite_notificare(
            "Backup Oprit",
            f"Sursa pare goală ({total} fișiere pe stick ar fi șterse)",
            iconita="dialog-error",
        )
        pauza_finala()
        sys.exit(1)

    if total >= 5 and procent > max_delete_percent:
        print()
        print(banner("BACKUP OPRIT", None, latime=70, culoare="rosu"))
        print(colorat("Prea multe ștergeri detectate.", "rosu"))
        print()
        print(f"  Stick:              {total} fișiere")
        print(f"  Ar fi șterse:       {colorat(f'{stergeri} ({procent:.0f}%)', 'magenta')}")
        print(f"  Prag configurat:    {max_delete_percent}%")
        print()
        print(colorat("Dacă e intenționat, rulează cu --max-delete-percent 100.", "cyan"))
        print(colorat("Nicio modificare nu a fost aplicată pe HDD.", "galben"))
        trimite_notificare(
            "Backup Oprit",
            f"Prea multe ștergeri: {stergeri}/{total} ({procent:.0f}%)",
            iconita="dialog-error",
        )
        pauza_finala()
        sys.exit(1)


# ==========================================
# PLANIFICARE (Pas I - read-only)
# ==========================================
def indexeaza_sursa(sursa_abs):
    fisiere_exacte = set()
    dupa_hash = {}
    contor = 0

    for root, _, files in walk_filtrat(sursa_abs):
        for f in files:
            cale_abs = os.path.join(root, f)
            cale_rel = os.path.relpath(cale_abs, sursa_abs)
            fisiere_exacte.add(cale_rel)
            contor += 1

            if contor % 1000 == 0:
                print(f"\r    {EMOJI['index']} Indexare... {contor} fișiere", end="", flush=True)

            amprenta = calculeaza_hash_rapid(cale_abs)
            if amprenta:
                dupa_hash.setdefault(amprenta, []).append(cale_rel)

    if contor >= 1000:
        print()

    return fisiere_exacte, dupa_hash, contor


def calculeaza_spatiu_necesar(sursa_abs, destinatia_folder):
    total = 0

    for root, _, files in walk_filtrat(sursa_abs):
        for f in files:
            cale_abs = os.path.join(root, f)
            cale_rel = os.path.relpath(cale_abs, sursa_abs)
            cale_hdd = os.path.join(destinatia_folder, cale_rel)

            try:
                st_sursa = os.stat(cale_abs)
                marime_sursa = st_sursa.st_size

                st_hdd = None
                if os.path.exists(cale_hdd):
                    try:
                        st_hdd = os.stat(cale_hdd)
                    except OSError:
                        st_hdd = None

                if st_hdd is None:
                    total += marime_sursa
                elif (st_hdd.st_size != marime_sursa
                      or st_hdd.st_mtime_ns != st_sursa.st_mtime_ns):
                    total += marime_sursa + st_hdd.st_size

            except OSError:
                pass

    return total


# ==========================================
# EXECUȚIE (Pas III - modifică HDD)
# ==========================================
def _trateaza_sters(cale_hdd_abs, cale_rel, dir_sterse, is_dry_run, contoare):
    dest_sterse_abs = os.path.join(dir_sterse, cale_rel)

    if is_dry_run:
        print(f"    {colorat('[SIMULARE]', 'galben')} {EMOJI['stergere']} {cale_rel} → _STERS/")
        contoare["sterse"] += 1
    else:
        try:
            os.makedirs(os.path.dirname(dest_sterse_abs), exist_ok=True)
            shutil.move(cale_hdd_abs, dest_sterse_abs)
            print(f"    {colorat(EMOJI['stergere'], 'magenta')} {cale_rel} → _STERS/")
            contoare["sterse"] += 1
        except OSError as e:
            print(colorat(f"    {EMOJI['eroare']} Eroare ștergere {cale_rel}: {e}", "rosu"))


def detecteaza_mutari_si_stergeri(
    destinatia_folder, sursa_abs,
    fisiere_sursa_exacte, fisiere_sursa_dupa_hash,
    dir_sterse, dir_modificate, is_dry_run, contoare,
):
    if not os.path.exists(destinatia_folder):
        return

    consumate = set()
    mutari_plasate = set()

    for root, _, files in walk_filtrat(destinatia_folder):
        for f in files:
            cale_hdd_abs = os.path.join(root, f)
            cale_rel = os.path.relpath(cale_hdd_abs, destinatia_folder)

            if cale_rel in fisiere_sursa_exacte:
                continue
            if cale_rel in mutari_plasate:
                continue

            amprenta_rapida = calculeaza_hash_rapid(cale_hdd_abs)
            if not amprenta_rapida:
                _trateaza_sters(cale_hdd_abs, cale_rel, dir_sterse, is_dry_run, contoare)
                continue

            hash_rapid_hdd, _ = amprenta_rapida
            if hash_rapid_hdd == "empty_file":
                _trateaza_sters(cale_hdd_abs, cale_rel, dir_sterse, is_dry_run, contoare)
                continue

            candidati = [
                c for c in fisiere_sursa_dupa_hash.get(amprenta_rapida, [])
                if (amprenta_rapida, c) not in consumate
            ]

            cale_noua_rel = None
            if candidati:
                hash_complet_hdd = calculeaza_hash_complet(cale_hdd_abs)
                for candidat in candidati:
                    cale_sursa_candidat = os.path.join(sursa_abs, candidat)
                    hash_complet_sursa = calculeaza_hash_complet(cale_sursa_candidat)
                    if not (hash_complet_hdd and hash_complet_hdd == hash_complet_sursa):
                        continue

                    tinta_hdd_abs = os.path.join(destinatia_folder, candidat)
                    if os.path.exists(tinta_hdd_abs):
                        hash_tinta = calculeaza_hash_complet(tinta_hdd_abs)
                        if hash_tinta == hash_complet_hdd:
                            continue

                    cale_noua_rel = candidat
                    break

            if cale_noua_rel:
                consumate.add((amprenta_rapida, cale_noua_rel))
                cale_noua_hdd_abs = os.path.join(destinatia_folder, cale_noua_rel)

                if is_dry_run:
                    if os.path.exists(cale_noua_hdd_abs):
                        print(f"    {colorat('[SIMULARE]', 'galben')} {EMOJI['mutare']} "
                              f"{cale_rel} → {cale_noua_rel} (țintă existentă → _MODIF)")
                    else:
                        print(f"    {colorat('[SIMULARE]', 'galben')} {EMOJI['mutare']} "
                              f"{cale_rel} → {cale_noua_rel}")
                    contoare["mutate"] += 1
                    mutari_plasate.add(cale_noua_rel)
                else:
                    try:
                        if os.path.exists(cale_noua_hdd_abs):
                            cale_modif_abs = os.path.join(dir_modificate, cale_noua_rel)
                            os.makedirs(os.path.dirname(cale_modif_abs), exist_ok=True)
                            shutil.move(cale_noua_hdd_abs, cale_modif_abs)

                        os.makedirs(os.path.dirname(cale_noua_hdd_abs), exist_ok=True)
                        shutil.move(cale_hdd_abs, cale_noua_hdd_abs)

                        cale_sursa_laptop = os.path.join(sursa_abs, cale_noua_rel)
                        try:
                            st = os.stat(cale_sursa_laptop)
                            os.utime(cale_noua_hdd_abs, (st.st_atime, st.st_mtime))
                        except OSError:
                            pass

                        print(f"    {colorat(EMOJI['mutare'], 'cyan')} {cale_rel} → {cale_noua_rel}")
                        contoare["mutate"] += 1
                        mutari_plasate.add(cale_noua_rel)
                    except OSError as e:
                        print(colorat(f"    {EMOJI['eroare']} Eroare mutare {cale_rel}: {e}", "rosu"))
            else:
                _trateaza_sters(cale_hdd_abs, cale_rel, dir_sterse, is_dry_run, contoare)


def ruleaza_rsync(sursa_abs, destinatia_folder, dir_modificate, is_dry_run, verbose=False):
    if not is_dry_run:
        try:
            os.makedirs(destinatia_folder, exist_ok=True)
        except OSError as e:
            print(colorat(f"    {EMOJI['eroare']} Nu pot crea destinația {destinatia_folder}: {e}", "rosu"))
            return 1, {}

    # -r recursive, -t times, -l symlinks (păstrează symlinks ca symlinks)
    cmd = ["rsync", "-rtl"]

    if verbose:
        cmd.extend(["-v", "--info=progress2,stats2,name"])
    else:
        cmd.extend(["--info=name,stats2"])

    cmd.extend([
        "--backup", f"--backup-dir={dir_modificate}",
        f"{sursa_abs}/", f"{destinatia_folder}/",
    ])
    for p in EXCLUDERI_FISIERE + EXCLUDERI_DIRECTOARE:
        cmd.extend(["--exclude", glob_ci(p)])

    if is_dry_run:
        cmd.append("--dry-run")

    # LC_ALL=C: forțează mesajele rsync în engleză (parsare robustă la locale)
    env_rsync = os.environ.copy()
    env_rsync["LC_ALL"] = "C"

    try:
        rezultat = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=TIMEOUT_RSYNC_SEC, env=env_rsync,
        )
    except subprocess.TimeoutExpired:
        print(colorat(
            f"    {EMOJI['eroare']} rsync timeout după {TIMEOUT_RSYNC_SEC}s "
            f"(disc blocat sau HDD decuplat?)",
            "rosu",
        ))
        return 1, {}

    if rezultat.stdout:
        print(rezultat.stdout, end="")
    if rezultat.stderr:
        print(rezultat.stderr, end="", file=sys.stderr)

    statistici = {}
    for linie in rezultat.stdout.splitlines():
        # Doar breakdown-ul (reg: N) — fișiere obișnuite, fără directoare.
        # Fallback la numărul brut NU se face: include directoare și ar
        # produce statistici greșite (modificate negative).
        m = re.search(r"Number of created files:\s*\d+\s*\(reg:\s*(\d+)", linie)
        if m:
            statistici["create"] = int(m.group(1))
            continue
        m = re.search(r"Number of regular files transferred:\s*(\d+)", linie)
        if m:
            statistici["transferate"] = int(m.group(1))

    return rezultat.returncode, statistici


# ==========================================
# CURĂȚARE
# ==========================================
def curata_istoric_user(dir_istoric_user, zile, dir_sesiune_curenta=None):
    if not os.path.isdir(dir_istoric_user):
        return 0

    prag = time.time() - zile * 86400
    sterse = 0
    dir_curenta_abs = os.path.abspath(dir_sesiune_curenta) if dir_sesiune_curenta else None

    for entry in os.listdir(dir_istoric_user):
        cale = os.path.join(dir_istoric_user, entry)
        if not os.path.isdir(cale):
            continue

        if dir_curenta_abs and os.path.abspath(cale) == dir_curenta_abs:
            continue

        try:
            ts = datetime.strptime(entry, FORMAT_TIMESTAMP_SESIUNE)
            vechime = ts.timestamp()
        except ValueError:
            try:
                vechime = os.path.getmtime(cale)
            except OSError:
                continue

        if vechime < prag:
            try:
                shutil.rmtree(cale, ignore_errors=True)
                sterse += 1
            except OSError:
                pass

    return sterse


def curata_foldere_goale(radacina):
    if not os.path.isdir(radacina):
        return

    for root, dirs, files in os.walk(radacina, topdown=False):
        for d in dirs:
            cale = os.path.join(root, d)
            try:
                if not os.listdir(cale):
                    os.rmdir(cale)
            except OSError:
                pass


def curata_foldere_goale_destinatie(destinatia_folder, dir_sterse, dir_modificate):
    for cale in (destinatia_folder, dir_sterse, dir_modificate):
        if cale and os.path.isdir(cale):
            curata_foldere_goale(cale)


# ==========================================
# SUMAR
# ==========================================
def afiseaza_sumar(latime, culoare_final, plan, contoare, istoric_sters,
                   durata_str, spatiu_necesar_mb, spatiu_liber_mb, erori_rsync):
    def linie(eticheta, valoare, valoare_colorata=None):
        if valoare_colorata is None:
            valoare_colorata = valoare

        text_simplu = f"  {eticheta:<22} {valoare}"
        lungime_reala = lungime_vizuala(text_simplu)
        padding = latime - 2 - lungime_reala
        if padding < 0:
            padding = 0

        text_afisat = f"  {eticheta:<22} {valoare_colorata}"
        return (
            colorat("║", culoare_final)
            + text_afisat
            + " " * padding
            + colorat("║", culoare_final)
        )

    def linie_titlu(text):
        lung = lungime_vizuala(text)
        padding = (latime - 2 - lung) // 2
        rest = latime - 2 - lung - padding
        return colorat("║", culoare_final) + " " * padding + text + " " * rest + colorat("║", culoare_final)

    print(banner("BACKUP FINALIZAT CU ERORI" if erori_rsync else "BACKUP FINALIZAT",
                 None, latime=latime, culoare=culoare_final))

    print(colorat("╔" + "═" * (latime - 2) + "╗", culoare_final))
    print(linie_titlu(f"{EMOJI['sumar']}  SUMAR"))
    print(colorat("╠" + "═" * (latime - 2) + "╣", culoare_final))

    print(linie("Surse procesate:", f"{len(plan)}"))
    print(linie("Fișiere noi:", str(contoare["noi"]), colorat(str(contoare["noi"]), "verde")))
    print(linie("Fișiere modificate:", str(contoare["modificate"]), colorat(str(contoare["modificate"]), "galben")))
    print(linie("Fișiere mutate:", str(contoare["mutate"]), colorat(str(contoare["mutate"]), "cyan")))
    print(linie("Fișiere șterse:", str(contoare["sterse"]), colorat(str(contoare["sterse"]), "magenta")))

    if istoric_sters:
        print(linie("Sesiuni istoric șterse:", str(istoric_sters)))

    print(colorat("╠" + "═" * (latime - 2) + "╣", culoare_final))
    print(linie("Durată:", durata_str))
    print(linie("Date transferate:", f"{spatiu_necesar_mb:.2f} MB"))
    print(linie("Spațiu rămas:", f"{spatiu_liber_mb:.1f} MB"))

    if erori_rsync:
        print(colorat("╠" + "═" * (latime - 2) + "╣", culoare_final))
        print(linie("Erori rsync:", str(len(erori_rsync)), colorat(str(len(erori_rsync)), "rosu")))

    print(colorat("╚" + "═" * (latime - 2) + "╝", culoare_final))


def afiseaza_locatii(plan, dir_sesiune_curenta, erori_rsync, is_dry_run):
    if is_dry_run:
        return

    print()

    if len(plan) == 1:
        destinatie_folder = plan[0][1]
        print(f"{EMOJI['locatie']} {colorat('Backup:', 'bold')} {destinatie_folder}/")
    else:
        print(f"{EMOJI['locatie']} {colorat('Backup:', 'bold')}")
        for entry in plan:
            destinatie_folder = entry[1]
            print(f"   {destinatie_folder}/")

    if not erori_rsync:
        print(f"{EMOJI['locatie']} {colorat('Istoric:', 'bold')} {dir_sesiune_curenta}/")


# ==========================================
# MAIN
# ==========================================
def main():
    start_time = time.time()
    args = parse_args()

    script_name = os.path.basename(sys.argv[0])

    if len(args.cai) < 2:
        print(colorat(f"{EMOJI['eroare']} EROARE: Specifică cel puțin o sursă și o destinație!\n", "rosu"))
        print(colorat("Exemple:", "cyan"))
        print(f"  ./{script_name} ~/Desktop ~/Documents /media/dan/stick")
        print(f"  ./{script_name} ~/Desktop /media/dan/stick --dry-run")
        print(f"  ./{script_name} ~/Desktop /media/dan/stick --verbose\n")
        pauza_finala()
        sys.exit(1)

    verifica_mediu()

    is_dry_run = args.dry_run
    verbose = args.verbose
    max_delete_percent = args.max_delete_percent
    allow_internal = args.allow_internal
    user_curent = getpass.getuser()
    hostname = socket.gethostname()

    destinatia_baza = os.path.abspath(os.path.expanduser(args.cai[-1]))
    foldere_sursa = [
        os.path.abspath(os.path.expanduser(p)) for p in args.cai[:-1]
    ]

    verifica_surse_si_destinatie(foldere_sursa, destinatia_baza)
    verifica_destinatie(destinatia_baza, foldere_sursa, is_dry_run, allow_internal)

    lock_fd = None
    try:
        lock_fd, lock_info = achizitioneaza_lock(destinatia_baza)
        if lock_fd is None:
            print()
            print(banner("BACKUP OPRIT", None, latime=70, culoare="rosu"))
            print(colorat("Altă instanță a scriptului rulează deja pe acest stick.", "rosu"))
            print()
            if lock_info:
                print(f"  {EMOJI['lock']} Deținut de: {colorat(lock_info, 'cyan')}")
            print()
            print(colorat("Așteaptă să se termine sau verifică procesul indicat.", "cyan"))
            print(colorat("Dacă ești sigur că nu rulează nimic, șterge lock-ul manual:", "gri"))
            print(colorat(f"  rm {get_lock_path(destinatia_baza)}", "gri"))
            trimite_notificare(
                "Backup Oprit",
                f"Altă instanță rulează: {lock_info}",
                iconita="dialog-error",
            )
            pauza_finala()
            sys.exit(1)

        baza_laptop = os.path.join(destinatia_baza, hostname)
        dir_istoric_baza = os.path.join(baza_laptop, "_Istoric")
        dir_istoric_user = os.path.join(dir_istoric_baza, "home", user_curent)

        acum = datetime.now().strftime(FORMAT_TIMESTAMP_SESIUNE)
        dir_sesiune_curenta = os.path.join(dir_istoric_user, acum)
        dir_sterse_sesiune = os.path.join(dir_sesiune_curenta, "_STERS")
        dir_modificate_sesiune = os.path.join(dir_sesiune_curenta, "_MODIF")

        _, _, free = shutil.disk_usage(destinatia_baza)
        spatiu_liber_gb = free / (1024 ** 3)

        titlu = "BACKUP SIMULARE" if is_dry_run else "BACKUP INTELIGENT"
        subtitlu = f"{EMOJI['laptop']} {hostname}  |  {EMOJI['spatiu']} {spatiu_liber_gb:.1f} GB liberi"
        print(banner(titlu, subtitlu, latime=60, culoare="galben" if is_dry_run else "cyan"))

        if is_dry_run:
            trimite_notificare("Backup SIMULARE", f"A început simularea backup-ului pe {hostname}.")
        else:
            trimite_notificare(
                "Backup Inițializat",
                f"Sincronizare pe HDD de pe {hostname} ({spatiu_liber_gb:.1f} GB liberi).",
            )

        # ==========================================
        # PASUL I: Planificare (read-only pe HDD)
        # ==========================================
        spatiu_total_necesar = 0
        plan = []
        contoare = {"noi": 0, "modificate": 0, "mutate": 0, "sterse": 0}
        total_surse = len([s for s in foldere_sursa if os.path.exists(s)])
        idx_sursa = 0

        for sursa_abs in foldere_sursa:
            if not os.path.exists(sursa_abs):
                print()
                print(colorat(f"{EMOJI['avert']} Folderul sursă nu există: {sursa_abs} (se omite)", "galben"))
                continue

            idx_sursa += 1
            cale_relativa = sursa_abs.lstrip(os.sep)
            destinatia_folder = os.path.join(baza_laptop, cale_relativa)

            dir_sterse = os.path.join(dir_sterse_sesiune, cale_relativa)
            dir_modificate = os.path.join(dir_modificate_sesiune, cale_relativa)

            print()
            print(linie_separator())
            print(f"{EMOJI['folder']} {colorat(f'[{idx_sursa}/{total_surse}]', 'bold')} {colorat(sursa_abs, 'cyan')}")
            print(linie_separator())

            print(f"  {colorat('[1/3]', 'gri')} {EMOJI['index']} Indexare fișiere...")
            fisiere_exacte, dupa_hash, nr_fisiere = indexeaza_sursa(sursa_abs)
            print(f"         {colorat(f'{nr_fisiere} fișiere indexate', 'gri')}")

            print(f"  {colorat('[2/3]', 'gri')} {EMOJI['scut']} Verificare siguranță ștergeri...")
            verifica_siguranta_stergeri(
                fisiere_exacte, dupa_hash,
                destinatia_folder, max_delete_percent,
            )
            print(f"         {colorat('OK', 'verde')}")

            spatiu_sursa = calculeaza_spatiu_necesar(sursa_abs, destinatia_folder)
            spatiu_total_necesar += spatiu_sursa
            print(f"  {colorat('[3/3]', 'gri')} {EMOJI['spatiu']} Spațiu necesar: "
                  f"{colorat(f'{spatiu_sursa / (1024**2):.2f} MB', 'cyan')}")

            plan.append((
                sursa_abs,
                destinatia_folder,
                dir_modificate,
                dir_sterse,
                fisiere_exacte,
                dupa_hash,
            ))

        # ==========================================
        # PASUL II: Verificare spațiu agregat
        # ==========================================
        _, _, free = shutil.disk_usage(destinatia_baza)
        marja_bytes = args.marja_mb * 1024 * 1024
        necesar_total = spatiu_total_necesar + marja_bytes

        spatiu_necesar_mb = spatiu_total_necesar / (1024 ** 2)
        spatiu_liber_mb = free / (1024 ** 2)

        print()
        print(linie_separator())
        print(f"{EMOJI['spatiu']} {colorat('VERIFICARE SPAȚIU', 'bold')}")
        print(linie_separator())
        print(f"  Date noi/modificate: {colorat(f'{spatiu_necesar_mb:.2f} MB', 'cyan')}")
        print(f"  Marjă de siguranță:  {colorat(f'{args.marja_mb} MB', 'gri')}")
        print(f"  Spațiu liber:        {colorat(f'{spatiu_liber_mb:.2f} MB', 'verde')}")

        if free < necesar_total:
            msg = (
                f"Spațiu insuficient! Necesar: {spatiu_necesar_mb:.1f} MB + {args.marja_mb} MB marjă, "
                f"liber: {spatiu_liber_mb:.1f} MB"
            )
            print()
            print(banner("BACKUP EȘUAT", msg, latime=70, culoare="rosu"))
            print(colorat("Sincronizarea a fost OPRITĂ pentru a preveni umplerea discului.", "galben"))
            print(colorat("Nicio modificare nu a fost aplicată pe HDD.", "galben"))
            durata_esec = time.time() - start_time
            trimite_notificare(
                "Backup Eșuat",
                f"{msg} (după {format_durata(durata_esec)})",
                iconita="dialog-error",
            )
            pauza_finala()
            sys.exit(1)

        # ==========================================
        # PASUL III: Execuție (modifică HDD-ul)
        # ==========================================
        print()
        print(linie_separator())
        print(f"{EMOJI['sync']} {colorat('EXECUȚIE BACKUP', 'bold')}")
        print(linie_separator())

        erori_rsync = []
        total_create = 0
        total_transferate = 0

        for idx_exec, entry in enumerate(plan, 1):
            sursa_abs, destinatia_folder, dir_modificate, dir_sterse, fisiere_exacte, dupa_hash = entry

            print()
            print(f"{EMOJI['folder']} {colorat(f'[{idx_exec}/{len(plan)}]', 'bold')} {colorat(sursa_abs, 'cyan')}")

            print(f"    {EMOJI['sync']} Detecție mutări/ștergeri...")
            mutari_inainte = contoare["mutate"]
            stergeri_inainte = contoare["sterse"]
            detecteaza_mutari_si_stergeri(
                destinatia_folder, sursa_abs,
                fisiere_exacte, dupa_hash,
                dir_sterse, dir_modificate,
                is_dry_run, contoare,
            )
            mutari_sursa = contoare["mutate"] - mutari_inainte
            stergeri_sursa = contoare["sterse"] - stergeri_inainte
            if mutari_sursa == 0 and stergeri_sursa == 0:
                print(f"       {colorat('Nicio mutare sau ștergere.', 'gri')}")
            else:
                print(f"       {colorat(f'{mutari_sursa} mutate, {stergeri_sursa} șterse', 'gri')}")

            print(f"    {EMOJI['sync']} Sincronizare rsync...")
            cod, statistici = ruleaza_rsync(
                sursa_abs, destinatia_folder, dir_modificate, is_dry_run, verbose
            )

            total_create += statistici.get("create", 0)
            total_transferate += statistici.get("transferate", 0)

            if cod != 0:
                if cod == 24:
                    print(colorat(f"       {EMOJI['avert']} rsync a returnat {cod} ({explica_eroare_rsync(cod)})", "galben"))
                else:
                    erori_rsync.append((sursa_abs, cod))
                    print(colorat(f"       {EMOJI['eroare']} rsync a returnat codul {cod}: {explica_eroare_rsync(cod)}", "rosu"))

            if not is_dry_run:
                curata_foldere_goale_destinatie(
                    destinatia_folder, dir_sterse, dir_modificate
                )

        contoare["noi"] = total_create
        contoare["modificate"] = max(0, total_transferate - total_create)

        # ==========================================
        # Curățare istoric
        # ==========================================
        istoric_sters = 0
        if not is_dry_run:
            curata_foldere_goale(dir_sesiune_curenta)

            print()
            print(f"  {EMOJI['istoric']} Curățare istoric > {args.zile_istoric} zile pentru {user_curent}...")
            istoric_sters = curata_istoric_user(
                dir_istoric_user, args.zile_istoric, dir_sesiune_curenta
            )
            if istoric_sters:
                print(f"         {colorat(f'{istoric_sters} sesiuni vechi șterse', 'gri')}")

        # ==========================================
        # Calcul durată
        # ==========================================
        durata = time.time() - start_time
        durata_str = format_durata(durata)

        # ==========================================
        # Notificare finală + SUMAR
        # ==========================================
        if erori_rsync:
            msg = f"Backup cu erori la: {', '.join(s for s, _ in erori_rsync)} ({durata_str})"
            trimite_notificare("Backup cu Erori", msg, iconita="dialog-warning")
            culoare_final = "rosu"
        elif is_dry_run:
            trimite_notificare("Simulare Finalizată", f"Simularea pe {hostname} s-a încheiat ({durata_str}).")
            culoare_final = "galben"
        else:
            trimite_notificare(
                "Backup Finalizat",
                f"Sincronizarea pe {hostname} s-a încheiat cu succes ({durata_str}).",
            )
            culoare_final = "verde"

        latime_sumar = 54
        print()
        afiseaza_sumar(
            latime_sumar, culoare_final, plan, contoare, istoric_sters,
            durata_str, spatiu_necesar_mb, spatiu_liber_mb, erori_rsync,
        )

        afiseaza_locatii(plan, dir_sesiune_curenta, erori_rsync, is_dry_run)

        print()
        pauza_finala()

        exit_code = 1 if erori_rsync else 0

    finally:
        elibereaza_lock(lock_fd)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
