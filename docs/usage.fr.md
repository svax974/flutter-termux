<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 svax974 -->

# Flutter pour Termux, depuis les sources

Lanceur Python 3.11+ utilisable depuis **bash ou zsh**, sans module Python à installer sur la machine de lancement. Il orchestre la compilation d'un SDK **Termux natif ARM64**, avec Dart et les outils AOT Android, et produit un `.deb` et ses empreintes SHA256.

**État de validation :** les tests automatisés du lanceur et du scénario de compilation simulé passent. Une compilation complète du moteur, une exécution SSH réelle et le test du paquet sur téléphone restent à effectuer. Aucune garantie de compatibilité des correctifs avec une version Flutter future : l'outil sélectionne la dernière publication officielle et arrête la procédure si le backend, les correctifs ou les ressources ne conviennent plus. Il ne se rabat jamais automatiquement sur une ancienne version.

## Démarrer

```bash
git clone https://github.com/svax974/flutter-termux.git
cd flutter-termux
cp .env.example .env
# Modifier .env avec l'éditeur de votre choix.
python3 flutter_termux.py check
python3 flutter_termux.py build
```

`check` contrôle le système hôte, le disque, la RAM, le swap et les dépendances système sans les installer. Il renvoie `2` si des dépendances système manquent, `1` en cas d'erreur bloquante, `0` si ces contrôles passent. Le NDK exact est déterminé à partir du backend pendant le build ; `check` n'annonce donc pas qu'un SDK Android complet est prêt.

`build` récupère le catalogue officiel Flutter, propose les cinq dernières versions stables et présélectionne la plus récente. Le choix **6. Saisir une autre version** permet d'indiquer une version plus ancienne, vérifiée dans le catalogue du canal sélectionné. Entrée valide la sélection par défaut ; une confirmation précède le lancement. Les installations manquantes sont proposées ensuite. L'acceptation des licences Android fait l'objet d'une confirmation distincte.

Pour conserver ce menu tout en évitant les questions d'installation, ajouter dans `.env` :

```dotenv
AUTO_INSTALL=true
ACCEPT_ANDROID_LICENSES=true
```

La deuxième valeur vaut acceptation des licences Android pour les installations lancées par cet outil. Ces réglages n'enlèvent pas les contrôles de disque, d'architecture ou de cohérence des versions. L'équivalent ponctuel est `--auto-install --accept-android-licenses`. `--yes` supprime aussi le menu de version et sélectionne la dernière.

Ne pas exécuter `source .env` : le fichier est lu comme des données. Les variables exportées dans l'environnement ont priorité sur `.env`, et les arguments ont priorité sur la configuration correspondante. Les chaînes `${VAR}` et `$(commande)` ne sont pas interprétées.

## Choisir la machine de compilation

| Cible | Configuration | Conditions |
| --- | --- | --- |
| Linux natif | `BUILD_TARGET=local` | Ubuntu 24.04 x86-64 recommandé ; Debian 12 x86-64 possible, non validé en compilation réelle |
| Serveur Linux | `BUILD_TARGET=ssh` | Même environnement Linux, Python 3.11+, accès SSH par clé/agent |
| macOS Intel ou Apple Silicon | `BUILD_TARGET=docker`, ou `local` automatiquement converti en Docker | Docker Desktop ou Colima avec Docker démarré ; conteneur Linux x86-64 |
| Linux ARM64 | `BUILD_TARGET=docker` | Émulation Linux x86-64 configurée dans Docker, ou serveur x86-64 via SSH |

Le mode macOS compile dans **Linux**, et non directement avec Xcode. Sur Apple Silicon il utilise l'émulation `linux/amd64`, qui peut être très lente. Un serveur Linux x86-64 est préférable pour une première compilation lourde.

### Serveur SSH

Exemple `.env` :

```dotenv
BUILD_TARGET=ssh
SSH_HOST=build.example.net
SSH_USER=builder
SSH_PORT=22
SSH_IDENTITY_FILE=~/.ssh/id_ed25519
WORK_DIR=/home/builder/flutter-termux-build
MIN_FREE_GIB=150
DISK_RESERVE_GIB=10
JOBS=1
```

Un alias de `~/.ssh/config` convient pour `SSH_HOST`. `ProxyJump`, l'agent SSH et la vérification des clés d'hôte restent ceux d'OpenSSH. `SSH_PORT` force le port configuré ici. Vérifier d'abord la connexion, notamment pour enregistrer la clé d'hôte si nécessaire :

```bash
ssh -p 22 builder@build.example.net
```

Le lanceur emploie `BatchMode=yes` et n'enregistre aucun mot de passe. Les installations système utilisent `sudo -n` si l'utilisateur distant n'est pas root : il faut un sudo sans interaction autorisé pour les commandes nécessaires, ou installer les paquets proposés depuis une session administrateur puis relancer. Python 3.11+ est le prérequis de transport, à installer sur le serveur avant le premier lancement.

Le contrôle d'espace est fait sur le système de fichiers de `WORK_DIR`, **avant le transfert du worker et le téléchargement des sources**. Le worker est transféré automatiquement ; les identifiants SSH et le `.env` complet restent sur la machine de lancement. Ne pas réutiliser le dossier d'un ancien checkout manuel : choisir un `WORK_DIR` dédié.

Garder la session de lancement ouverte. Le mode SSH est suivi au premier plan ; il ne fournit pas un service de compilation détaché. Pour supporter la fermeture du terminal, lancer le contrôleur dans `tmux` sur une machine qui reste connectée. Les étapes terminées sont reprises avec `--resume`.

### Linux local

```bash
python3 flutter_termux.py check --target local
python3 flutter_termux.py build --target local
```

Le lanceur demande confirmation avant `apt-get update` et l'installation des paquets manquants. Il utilise un venv par version pour les dépendances du backend et n'installe rien avec `pip` dans le Python système.

### macOS / Docker

```bash
python3 flutter_termux.py build --target docker
```

La première exécution propose de construire l'image du [Dockerfile](../Dockerfile). Docker doit déjà être installé et son moteur démarré : l'installation de Docker Desktop/Colima n'est pas automatisée.

Le contexte Docker doit utiliser un socket Unix local. Si le contexte actif pointe vers un serveur distant, régler `DOCKER_CONTEXT=desktop-linux`, `colima` ou le nom du contexte local affiché par `docker context ls`. Le lanceur sélectionne ce contexte explicitement pour toutes ses commandes Docker, sans changer votre contexte global.

Les sources et intermédiaires sont conservés dans le volume Docker `flutter-termux-build`, sur un système de fichiers Linux sensible à la casse. Ils ne sont pas compilés sur le volume partagé macOS. Les `.deb`, SHA256 et preuves sont exportés dans `./artifacts/<version>/`.

Prévoir au moins 150 Gio **libres dans le disque Linux de Docker**, après construction de l'image. Le disque virtuel Docker et la RAM qui lui est allouée doivent être suffisamment grands ; l'espace libre du Mac seul ne suffit pas. `check --target docker` exige une image déjà construite. Pour préparer uniquement l'image :

```bash
docker --context desktop-linux build --platform linux/amd64 -t flutter-termux-builder:ubuntu24.04 .
python3 flutter_termux.py check --target docker
```

Les chemins `ANDROID_SDK_ROOT` / `ANDROID_NDK` fournis pour cette cible doivent exister **dans le conteneur**. Laisser ces variables vides pour une installation dans le volume `/work`, plutôt que d'indiquer le SDK macOS.

## Versions récentes et méthode retenue

La version par défaut vient de `current_release.stable` dans le [catalogue officiel Flutter](https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json), pas de la dernière release d'un fork Termux. L'[archive officielle](https://docs.flutter.dev/install/archive) décrit les canaux et les révisions publiées.

Le backend par défaut est [GeneralKaos666/flutter-for-termux](https://github.com/GeneralKaos666/flutter-for-termux), dont les instructions actuelles distinguent les compilations Linux et Android AOT. Le portage et ses correctifs restent maintenus dans ce dépôt amont, indépendamment du lanceur.

Pour chaque nouvelle version, le lanceur :

1. Épingle le commit du backend et de `depot_tools` dans le workspace.
2. Règle `build.toml` sur la version et le commit officiels Flutter.
3. Installe les dépendances Python dans un venv et provisionne le NDK hôte.
4. Clone les sources Flutter et vérifie leur commit avec le catalogue officiel.
5. Lit `flutter --version --machine` sur l'hôte Linux pour synchroniser Dart, DevTools et la date du commit. Ce bootstrap utilise les artefacts officiels **hôtes** uniquement pour obtenir les métadonnées ; les outils livrés à Termux sont compilés ensuite.
6. Lance `sync`, y compris les hooks de correctifs Bionic du backend, puis construit le sysroot Termux.
7. Configure et compile les moteurs Linux en **Release, Debug et Profile**, avec Release en premier.
8. Compile séparément les outils AOT Android **Release et Profile**.
9. Contrôle les sorties Linux attendues puis lance le résolveur et le packaging `debuild` du backend.
10. Inspecte le contenu réel du `.deb` : version, architecture, Dart/Dart VM/runtime, snapshot Dart et outils AOT Android ARM64 ; copie les paquets et écrit SHA256 et `build-evidence.json`.

Les anciennes commandes `patches` / `patch_*` ne sont pas rejouées après `sync` : les hooks s'en chargent dans ce backend. On n'invoque pas `build_all` ni `output` en guise de packaging. Aucun fichier manquant n'est supprimé d'un manifeste et aucun exécutable `flutter_linux` n'est remplacé par `libflutter_linux_gtk.so`.

La sélection d'une version Flutter nouvelle n'élimine pas le travail éventuel de portage des correctifs Bionic. En cas de conflit, le build s'arrête, les sources et les logs sont conservés. Pour intégrer des correctifs adaptés à une version récente, choisir son fork via `BACKEND_REPO` et `BACKEND_REF`, avec un nouveau `WORK_DIR`. Le contrat du backend doit conserver les commandes `clone`, `sync`, `sysroot`, `configure`, `build`, `configure_android`, `build_android`, `debuild`. Une commande peut être une méthode ou un objet appelable exposé à Python Fire. Après l'installation des dépendances et la configuration du NDK, le lanceur inspecte l'interface réelle de `Build()` avant de télécharger le moteur ; le contrôle statique initial ne rejette pas à tort une commande `sysroot` déléguée à un objet.

## Android SDK et NDK

L'outil utilise un NDK **Linux x86-64 pour la compilation croisée**. Il ne réutilise pas un NDK compilé pour Android/Termux ni le NDK macOS. Les noms sont proches, mais les exécutables hôtes diffèrent.

Si `ANDROID_NDK` désigne un NDK existant, son compilateur et `source.properties` sont contrôlés. Sinon, le lanceur résout la version indiquée par `NDK_VERSION` ou le backend dans les métadonnées officielles Google, propose d'installer les Command-Line Tools et le NDK et demande l'acceptation des licences. Les archives des Command-Line Tools sont vérifiées avec l'empreinte publiée par Google. Le NDK est installé par [`sdkmanager`](https://developer.android.com/tools/sdkmanager).

Si un fork référence un numéro de NDK repackagé qui n'existe pas chez Google pour l'hôte, le lanceur affiche une version officielle stable de **la même génération** et regroupe ce choix avec la confirmation d'installation. Une valeur `NDK_VERSION` fixée par l'utilisateur n'est jamais substituée. Le choix validé après installation est mémorisé dans `<SDK>/.flutter-termux-ndk.json` ; un NDK correspondant déjà installé est réutilisé. Ce fichier reste dans le volume Docker entre les conteneurs et les versions Flutter. L'emploi de ce NDK doit encore être validé par la compilation effective.

Pour compiler le SDK/moteur, on n'installe pas inutilement un émulateur Android, Android Studio ou tous les build-tools APK sur le serveur. Le SDK Android permettant ensuite de compiler des applications **sur le téléphone** est une installation distincte, assurée par la procédure Termux du backend.

## Ressources, progression et reprise

Le seuil de départ de **150 Gio libres** est conservateur pour cinq variantes de moteur et leur packaging ; ce n'est pas une mesure de consommation maximale. Il peut être ajusté dans `.env` ou avec `--min-free-gib`, en fonction de l'espace déjà occupé et de l'expérience du build. Le même seuil s'applique à une reprise. Une réserve de 10 Gio est surveillée pendant les commandes et les téléchargements ; son franchissement arrête le groupe de processus de compilation.

`JOBS=1` est le défaut pour réduire le risque de dépassement mémoire lors de l’édition des liens. L'option `--jobs` est transmise aux méthodes qui l'acceptent, et un wrapper Ninja ajouté au `PATH` impose la limite même si le backend ajoute un ancien `-j24`. Un backend modifié pour appeler un Ninja par chemin absolu peut contourner ce wrapper. Aucun swap ni `/etc/fstab` n'est modifié automatiquement ; prévoir une mémoire/swap suffisante pour l'édition des liens, même avec un seul job.

Le terminal montre les dix étapes, les commandes et la sortie réelle des outils. Une ligne de présence est émise toutes les dix secondes lorsqu'une commande reste silencieuse. `build.log` et `state.json` sont conservés dans :

```text
<WORK_DIR>/builds/<version>/
├── backend/                # checkout et sources Flutter
├── venv/
├── build.log
├── state.json
├── flutter-metadata.json
└── artifacts/<version>/    # sur Linux / SSH ; export local séparé dans Docker
```

Reprendre avec **la même version** (ne pas laisser la dernière changer entre deux exécutions) :

```bash
python3 flutter_termux.py build --version VERSION_CHOISIE --resume
```

Après perte d'un artefact de compilation ou réparation d'un problème de ressources :

```bash
python3 flutter_termux.py build --version VERSION_CHOISIE --resume --rebuild-from 8
```

Ninja réutilise ses objets existants. `--rebuild-from 10` relance uniquement la vérification et le packaging. Rejouer `sync` à l'étape 6 sur des sources déjà patchées peut provoquer des conflits : conserver le diagnostic et ne pas effacer arbitrairement les correctifs.

Le verrou du workspace empêche deux compilations simultanées dans le même `WORK_DIR`. Une configuration ou un commit Flutter différent exige un autre workspace ; le lanceur ne réinitialise pas un checkout existant. L'interruption normale du worker arrête les compilateurs enfants. Après perte SSH ou interruption brutale, vérifier qu'aucun worker précédent ne tourne avant une reprise.

## Autres commandes

```bash
# Afficher les publications officielles sans contacter la machine de compilation.
python3 flutter_termux.py versions
python3 flutter_termux.py versions --channel beta --count 10

# Dernière stable sans menu ; les licences Android restent une autorisation séparée.
python3 flutter_termux.py build --yes

# Automatisation avec acceptation explicite des licences Android.
python3 flutter_termux.py build --yes --accept-android-licenses

# Catalogue officiel déjà téléchargé (pas de garantie de fraîcheur du fichier).
python3 flutter_termux.py versions --releases-file releases_linux.json

# Aide et tests, sans compiler le moteur ni installer des paquets.
python3 flutter_termux.py --help
python3 -m unittest discover -s tests -v
```

`stable` est le canal par défaut ; `beta` est également proposé, sans garantie de compatibilité Termux. Le canal `main`, qui ne correspond pas à une collection de versions SDK publiées, n'est pas pris en charge.

## Valider sur le téléphone

La présence des fichiers ARM64 ne prouve pas leur bon fonctionnement sur Android. Après vérification du SHA256 et installation du `.deb` dans Termux, suivre les instructions du backend et tester au minimum :

```bash
flutter --version
dart --version
flutter doctor -v
flutter create validation_termux
cd validation_termux
flutter build apk --release --target-platform android-arm64
```

Tester également le debug/hot reload et, si utilisé, `flutter build linux` dans les modes inclus avec Termux:X11. Le fichier de preuves indique volontairement `termux_device_test: not_run` jusqu'à une validation réelle. Ne pas lancer `flutter upgrade` sur ce SDK patché : une mise à jour se fait en reconstruisant et réinstallant le paquet.
