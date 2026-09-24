# backup_ext4

Script Python de backup inteligent pentru Linux, folosind `rsync`, cu detecție de fișiere mutate/redenumite, istoric al modificărilor/ștergerilor și verificări de siguranță menite să prevină umplerea discului sau ștergerea accidentală a datelor.

Gândit pentru sincronizare de pe discul intern către un HDD/stick extern formatat **ext4** (păstrează permisiuni și timestamp-uri specifice Linux).

## Funcționalități

- **Sincronizare incrementală** cu `rsync` (doar fișierele noi/modificate sunt transferate).
- **Detecție de mutări/redenumiri**: dacă un fișier a fost redenumit sau mutat în sursă, scriptul îl recunoaște (după conținut, nu doar după nume) și îl mută corespunzător pe destinație, în loc să îl copieze din nou și să șteargă vechea copie.
- **Istoric per-sesiune**: fișierele șterse sau suprascrise nu sunt pierdute — sunt mutate într-un folder de istoric (`_STERS` / `_MODIF`), organizat pe utilizator și dată/oră.
- **Curățare automată a istoricului** mai vechi de N zile (implicit 30).
- **Verificări de siguranță înainte de a scrie orice pe disc**:
  - refuză să ruleze ca `root`;
  - verifică dacă destinația este montată și scriptibilă;
  - refuză implicit backup-ul pe discul intern (trebuie `--allow-internal` explicit);
  - refuză dacă sursa și destinația se suprapun;
  - **oprește backup-ul dacă un procent prea mare din fișierele de pe destinație ar fi șterse** (protecție împotriva unui HDD nemontat corect sau a unui folder golit din greșeală) — prag configurabil, implicit 50%;
  - verifică spațiul liber disponibil înainte de a începe transferul efectiv.
- **Lock de execuție** (`fcntl.flock`) — previne rularea simultană a două instanțe pe același stick.
- **Mod simulare** (`--dry-run`) — arată exact ce s-ar întâmpla, fără nicio modificare reală.
- Notificări desktop (`notify-send`), sumar colorat în terminal, izolare pe `hostname` (util dacă backup-ul e pornit de pe mai multe calculatoare pe același stick).

## Cerințe

- Linux, Python 3.8+
- `rsync` instalat (`sudo apt install rsync`)
- opțional: `notify-send` pentru notificări desktop (pachetul `libnotify-bin`)
- destinația trebuie să fie un filesystem separat (de regulă ext4), montat înainte de rulare

## Utilizare

```bash
./backup_ext4.py <sursă1> [<sursă2> ...] <destinație>
```

Ultimul argument este întotdeauna destinația; toate celelalte sunt foldere sursă.

### Exemple

```bash
# Backup simplu, un singur folder sursă
./backup_ext4.py ~/Documents /media/dan/stick

# Mai multe foldere sursă deodată
./backup_ext4.py ~/Desktop ~/Documents /media/dan/stick

# Simulare, fără nicio modificare pe disc
./backup_ext4.py ~/Documents /media/dan/stick --dry-run

# Output detaliat (listă completă rsync)
./backup_ext4.py ~/Documents /media/dan/stick --verbose
```

### Opțiuni

| Opțiune | Descriere | Implicit |
|---|---|---|
| `-n`, `--dry-run` | Simulare, fără modificări fizice | — |
| `-v`, `--verbose` | Output detaliat rsync (listing complet per fișier) | — |
| `--allow-internal` | Permite backup pe discul intern (nu doar pe filesystem extern) | — |
| `--max-delete-percent N` | Refuză backup-ul dacă peste N% din fișierele de pe destinație ar fi șterse (100 = dezactivează verificarea) | 50 |
| `--marja-mb N` | Marjă de siguranță de spațiu liber, în MB | 200 |
| `--zile-istoric N` | Zile de păstrare a istoricului de fișiere șterse/modificate | 30 |

## Structura pe destinație

```
<destinație>/
└── <hostname>/
    ├── <cale_absolută_sursă>/...          ← copia curentă (oglindă a sursei)
    └── _Istoric/
        └── home/<utilizator>/
            └── <YYYY-MM-DD_HH-MM-SS>/     ← o sesiune de backup
                ├── _STERS/...             ← fișiere care nu mai există în sursă
                └── _MODIF/...             ← versiunile vechi ale fișierelor suprascrise
```

Prefixul `<hostname>` izolează backup-urile provenite de pe calculatoare diferite pe același stick, iar istoricul este separat pe utilizator.

## Limitări cunoscute

- Legăturile simbolice (symlinks) **nu sunt copiate** — `rsync` este apelat fără `-l`/`--links`.
- Permisiunile și proprietarul fișierelor **nu sunt păstrate** pe destinație (doar timpul de modificare, via `-t`).

