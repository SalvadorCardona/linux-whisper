# CLAUDE.md

## Le projet

whisper-desk est une dictée vocale hors-ligne pour Linux, WSL et macOS : un raccourci
global ouvre un overlay, et le texte transcrit localement (faster-whisper) est inséré
au curseur, phrase après phrase. Aucune donnée ne quitte la machine ; la transcription
tourne sur GPU si une carte NVIDIA est détectée, sinon sur CPU.

## Stack

- Python 3.11+ pour tout le code du dépôt (`src/`, `tests/`) — bibliothèque standard
  uniquement, y compris `tomllib` pour la configuration. Pas de `pyproject.toml`, pas
  de gestionnaire de paquets au niveau du dépôt.
- `faster-whisper`, seule dépendance tierce du projet (avec `numpy`, qu'elle entraîne),
  n'est importée qu'en différé, à l'intérieur d'une fonction, dans `transcriber.py` —
  jamais en tête de module. Elle vit dans un venv isolé créé par `install.sh` via `uv`
  (Python 3.12), pas dans le Python système, et n'a donc pas besoin d'être installée
  pour que les tests tournent. `numpy` est optionnel et protégé par un `try/except` à
  l'import (`spectrum.py`) : absent, l'overlay retombe sur le volume global.
- Service utilisateur au démarrage : systemd sur Linux/WSL, launchd sur macOS.
- L'overlay (`overlay.py`) tourne sur le **Python système** (GTK3 via PyGObject), pas
  dans le venv : voir la section pièges plus bas.

## Commandes

Installer (crée le venv, installe faster-whisper, active le service et le raccourci) :

```sh
curl -LsSf https://raw.githubusercontent.com/SalvadorCardona/whisper-desk/main/install.sh | sh
```

Lancer la suite de tests (stdlib seule, aucun venv ni modèle requis) :

```sh
python3 -m unittest discover -s tests -t .
```

Il n'y a ni build, ni lint, ni typecheck configurés dans ce dépôt — ne pas en inventer.
La CI (`.github/workflows/tests.yml`) se limite aux tests ci-dessus, à
`python -m compileall -q src` et à `sh -n install.sh && sh -n uninstall.sh`.

## Arborescence utile

| Chemin | Rôle |
|---|---|
| `src/whisper_desk/` | le programme : daemon, capture, transcription, injection, overlay |
| `tests/` | suite `unittest`, `tests/context.py` rend le package importable sans l'installer |
| `bin/whisper-desk.in` | gabarit du binaire installé, `exec` le Python du venv |
| `systemd/whisper-desk.service.in` | gabarit d'unité systemd (Linux/WSL) |
| `launchd/fr.whisperdesk.daemon.plist.in` | gabarit d'agent launchd (macOS) |
| `desktop/` | gabarits des entrées du menu des applications et de l'autostart de l'indicateur |
| `docs/` | site de présentation (GitHub Pages), pas la documentation technique |
| `install.sh` / `uninstall.sh` | installation, mise à jour, désinstallation |
| `config.example.toml` | configuration commentée, miroir des valeurs par défaut de `config.py` |

Le README documente déjà l'usage, la configuration et le dépannage en détail — ne pas
le dupliquer ici.

## Conventions

- Chaque module commence par `from __future__ import annotations` et un docstring
  d'une ligne décrivant son rôle. Types explicites sur les signatures publiques.
- `logging.getLogger("whisper-desk.<module>")` par module ; pas de `print` hors CLI
  (`_print_error` dans `__main__.py`).
- Commentaires réservés au *pourquoi* (contrainte Wayland, interop WSL, etc.), jamais
  au *quoi*.
- Détection du host via `WD_HOST` (`linux` | `wsl` | `macos`), testable depuis
  n'importe lequel des trois grâce au gestionnaire de contexte `forced_host` de
  `tests/context.py`.
- Commits en français, style Conventional Commits : `type(scope): message au présent`,
  minuscule, sans point final — ex. `fix(hotkey): give a wedged dictation up with the
  shortcut`, `feat(update): update whisper-desk from the CLI`. `docs:` sans scope pour
  la documentation et le site.

## Pièges connus

- **Ne pas importer `faster_whisper` en tête de fichier** : cela casserait les tests
  et `compileall` sur toute machine sans le venv. L'import reste local à la fonction
  qui l'utilise, dans `transcriber.py`.
- **L'overlay tourne hors du venv.** `overlay.py` et `overlay_proc.py` s'exécutent
  avec le Python système (celui qui a PyGObject), jamais avec celui du venv : ne pas
  leur ajouter de dépendance qui ne serait installée que côté venv. Même règle pour
  toutes les fenêtres `*_window.py` (historique, réglages, accueil, indicateur), vues
  pures pilotées en JSON par leur `*_proc.py` côté venv, via `window_proc.py`.
- **`overlay_protocol.py` est importé par `overlay.py` comme un script voisin**, pas
  comme un module du package : stdlib seule, et aucun import relatif.
- **Le raccourci clavier installé une première fois n'est jamais réécrit** par
  `whisper-desk update` : ne pas modifier ce comportement en passant par une autre
  tâche.
- **Une dictée en cours bloque volontairement la mise à jour.** `update` refuse de
  s'exécuter tant qu'une transcription est en cours plutôt que de la couper.
- **`config.example.toml` et les valeurs par défaut de `config.py` doivent rester en
  miroir** : un changement dans `DEFAULTS` sans le répercuter dans l'exemple part en
  silence — `tests/test_config.py` le vérifie désormais.
- Les tests skippés (`numpy` absent, notamment) sont normaux dans cet environnement ;
  ce n'est pas un échec à corriger.
