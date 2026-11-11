#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 svax974

"""Compilation Flutter pour Termux : Linux local, SSH ou Docker (macOS inclus).

Contrôleur et worker utilisent exclusivement la bibliothèque standard Python.
Les dépendances du backend sont installées dans son propre environnement virtuel.
"""
from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

# Le catalogue est sous /releases/ ; /flutter/<revision>/ héberge les artefacts moteur.
RELEASES_URL = "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json"
ANDROID_REPOSITORY = "https://dl.google.com/android/repository/"
REPOSITORY_XML = ANDROID_REPOSITORY + "repository2-1.xml"
DEFAULT_REPO = "https://github.com/GeneralKaos666/flutter-for-termux.git"
BACKEND_COMMANDS = ("clone", "sync", "sysroot", "configure", "build",
                    "configure_android", "build_android", "debuild")
GIB = 1024 ** 3
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\Z")
PACKAGES = (
    "python3-venv", "python3-pip", "git", "curl", "ca-certificates",
    "build-essential", "ninja-build", "cmake", "pkg-config", "clang", "lld",
    "dpkg-dev", "binutils", "xz-utils", "unzip", "zip", "zstd", "file", "patch",
    "libglib2.0-dev", "libgtk-3-dev", "liblzma-dev", "libfontconfig1-dev",
    "libglu1-mesa-dev",
)


class BuildError(Exception):
    pass


def request(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "flutter-termux-builder/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        error.close()
        raise BuildError(f"Téléchargement impossible : {url} (HTTP {error.code} : {error.reason}).") from error
    except urllib.error.URLError as error:
        raise BuildError(f"Téléchargement impossible : {url} ({error.reason}).") from error


def releases(document: dict, channel: str, count: int | None = None) -> list[dict]:
    """Déduplique les architectures et respecte current_release, sans version figée."""
    current = document.get("current_release", {}).get(channel)
    records = {}
    for row in document["releases"]:
        if row.get("channel") != channel or row.get("dart_sdk_arch", "x64") != "x64":
            continue
        if not VERSION_RE.fullmatch(row.get("version", "")):
            continue
        previous = records.get(row["version"])
        if previous is None or row.get("release_date", "") > previous.get("release_date", ""):
            records[row["version"]] = row
    ordered = sorted(records.values(), key=lambda x: x.get("release_date", ""), reverse=True)
    ordered.sort(key=lambda x: x.get("hash") != current)
    if not ordered:
        raise BuildError(f"Aucune version officielle trouvée pour le canal {channel}.")
    return ordered[:count] if count else ordered


def read_env(path: Path) -> dict[str, str]:
    """Sous-ensemble .env explicite ; aucune expansion ni exécution shell."""
    result = {}
    if not path.exists():
        return result
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise BuildError(f"{path}:{number}: attendre CLE=valeur.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise BuildError(f"{path}:{number}: guillemet non fermé.")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[key] = value
    return result


def positive(value: str | float, name: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise BuildError(f"{name} doit être un nombre positif.") from None
    if not 0 < number < 100000:
        raise BuildError(f"{name} doit être un nombre positif fini.")
    return number


def boolean(value: str, name: str) -> bool:
    value = value.strip().lower()
    if value in ("true", "1", "yes", "on", "oui"):
        return True
    if value in ("false", "0", "no", "off", "non", ""):
        return False
    raise BuildError(f"{name} doit être true ou false.")


def settings(args) -> dict:
    env = read_env(Path(args.env))

    def get(key, default=""):
        return os.environ.get(key, env.get(key, default))

    target = args.target or get("BUILD_TARGET", "local")
    if target not in ("local", "ssh", "docker"):
        raise BuildError("BUILD_TARGET doit être local, ssh ou docker.")
    if target == "local" and platform.system() == "Darwin":
        target = "docker"
        print("macOS : compilation dans Linux x86-64 via Docker.", flush=True)
    config = {
        "target": target,
        "work_dir": args.work_dir or get("WORK_DIR", "~/.cache/flutter-termux"),
        "min_free_gib": positive(args.min_free_gib if args.min_free_gib is not None else get("MIN_FREE_GIB", "150"), "MIN_FREE_GIB"),
        "reserve_gib": positive(get("DISK_RESERVE_GIB", "10"), "DISK_RESERVE_GIB"),
        "backend_repo": get("BACKEND_REPO", DEFAULT_REPO),
        "backend_ref": get("BACKEND_REF", "main"),
        "sdk_root": get("ANDROID_SDK_ROOT"),
        "ndk": get("ANDROID_NDK"),
        "ndk_version": get("NDK_VERSION"),
        "auto_install": args.auto_install or boolean(get("AUTO_INSTALL", "false"), "AUTO_INSTALL"),
        "accept_android_licenses": args.accept_android_licenses or boolean(
            get("ACCEPT_ANDROID_LICENSES", "false"), "ACCEPT_ANDROID_LICENSES"),
        "resume": args.resume,
        "jobs": args.jobs if args.jobs is not None else int(get("JOBS", "1")),
        "check": args.command == "check",
        "docker_image": get("DOCKER_IMAGE", "flutter-termux-builder:ubuntu24.04"),
        "docker_volume": get("DOCKER_VOLUME", "flutter-termux-build"),
        "docker_context": get("DOCKER_CONTEXT"),
        "artifact_dir": str(Path(get("ARTIFACT_DIR", "./artifacts")).expanduser().resolve()),
        "ssh_host": get("SSH_HOST"),
        "ssh_user": get("SSH_USER"),
        "ssh_port": get("SSH_PORT", "22"),
        "ssh_identity": get("SSH_IDENTITY_FILE"),
    }
    if config["min_free_gib"] <= config["reserve_gib"]:
        raise BuildError("MIN_FREE_GIB doit dépasser DISK_RESERVE_GIB.")
    if not 1 <= config["jobs"] <= 1024:
        raise BuildError("JOBS doit être compris entre 1 et 1024.")
    if config["work_dir"].startswith("~") and not config["work_dir"].startswith("~/"):
        raise BuildError("WORK_DIR doit être absolu, relatif, ou commencer par ~/ (sans nom d'utilisateur).")
    if config["backend_ref"].startswith("-"):
        raise BuildError("BACKEND_REF ne peut pas commencer par un tiret.")
    for key in ("work_dir", "backend_ref", "backend_repo"):
        if not config[key] or "\n" in config[key] or "\x00" in config[key]:
            raise BuildError(f"Configuration invalide : {key}.")
    if not config["backend_repo"].startswith("https://"):
        raise BuildError("BACKEND_REPO doit être une URL HTTPS Git (sans identifiants).")
    from urllib.parse import urlsplit
    if urlsplit(config["backend_repo"]).username:
        raise BuildError("Ne pas placer d'identifiants dans BACKEND_REPO.")
    return config


def confirm(question: str, automatic=False) -> bool:
    if automatic:
        print(question + " oui (mode automatique)", flush=True)
        return True
    if not sys.stdin.isatty():
        raise BuildError("Confirmation nécessaire : utiliser un terminal ou --yes.")
    return input(question + " [o/N] ").strip().lower() in ("o", "oui", "y", "yes")


def select_release(args) -> dict:
    if args.releases_file:
        doc = json.loads(Path(args.releases_file).read_text())
    else:
        doc = json.loads(request(RELEASES_URL))
    rows = releases(doc, args.channel)
    if args.version:
        selected = next((x for x in rows if x["version"] == args.version), None)
        if selected is None:
            raise BuildError(f"Flutter {args.version} n'est pas publié sur le canal {args.channel}.")
    else:
        print(f"Dernières versions officielles ({args.channel}) :")
        visible = rows[:args.count]
        for i, row in enumerate(visible, 1):
            print(f"  {i}. Flutter {row['version']} — Dart {row.get('dart_sdk_version', '?')}"
                  f" — {row.get('release_date', '')[:10]}" + (" [défaut]" if i == 1 else ""))
        if args.command == "versions":
            return rows[0]
        if args.yes:
            selected = rows[0]
        else:
            if not sys.stdin.isatty():
                raise BuildError("Choix interactif : utiliser un terminal ou --yes.")
            custom_choice = str(len(visible) + 1)
            print(f"  {custom_choice}. Saisir une autre version")
            choice = input("Choix [1] : ").strip() or "1"
            if choice == custom_choice:
                version = input("Numéro de version Flutter à compiler (ex. 3.47.2) : ").strip()
                selected = next((x for x in rows if x["version"] == version), None)
                if selected is None:
                    raise BuildError(f"Flutter {version or '(vide)'} n'est pas publié sur le canal {args.channel}.")
            elif choice.isdigit() and 1 <= int(choice) <= len(visible):
                selected = rows[int(choice) - 1]
            else:
                selected = next((x for x in rows if x["version"] == choice), None)
                if selected is None:
                    raise BuildError("Sélection inconnue dans les publications officielles.")
    if not re.fullmatch(r"[a-f0-9]{40}", selected.get("hash", "")):
        raise BuildError("La publication officielle ne contient pas un commit Flutter valide.")
    return selected


def ssh_base(config: dict) -> list[str]:
    host, user = config["ssh_host"], config["ssh_user"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:\[\]-]*", host):
        raise BuildError("SSH_HOST manquant/invalide : utiliser un hôte ou un alias SSH, sans user@.")
    if user and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise BuildError("SSH_USER invalide.")
    try:
        port = int(config["ssh_port"])
    except ValueError:
        raise BuildError("SSH_PORT invalide.") from None
    if not 1 <= port <= 65535:
        raise BuildError("SSH_PORT invalide.")
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-p", str(port)]
    if config["ssh_identity"]:
        cmd += ["-i", str(Path(config["ssh_identity"]).expanduser())]
    return cmd + ["--", f"{user}@{host}" if user else host]


def checked(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, **kwargs)
    if result.returncode:
        raise BuildError(f"Commande en échec ({result.returncode}) : {shlex.join(argv[:3])}")
    return result


def remote_worker(config: dict) -> tuple[list[str], str]:
    """Dépose un worker unique après un contrôle disque sans mutation."""
    base = ssh_base(config)
    probe_code = (
        "import json,sys; from pathlib import Path; import shutil; "
        "p=Path(sys.argv[1]).expanduser().resolve(); "
        "p=next(x for x in [p,*p.parents] if x.exists()); "
        "print(json.dumps({'python':list(sys.version_info[:2]),'free':shutil.disk_usage(p).free}))"
    )
    probe = subprocess.run(base + [shlex.join(["python3", "-c", probe_code, config["work_dir"]])],
                           text=True, capture_output=True)
    if probe.returncode:
        raise BuildError("Précontrôle SSH impossible. Vérifier la connexion et Python 3.11+ sur le serveur.\n"
                         + probe.stderr.strip())
    report = json.loads(probe.stdout)
    if report["python"] < [3, 11]:
        raise BuildError("Python 3.11+ requis sur le serveur (Ubuntu 24.04 / Debian 12 conseillés).")
    require_space(report["free"], config["min_free_gib"], config["work_dir"])
    upload_code = (
        "import pathlib,sys,tempfile; "
        "p=pathlib.Path(tempfile.mkdtemp(prefix='flutter-termux-'))/'worker.py'; "
        "p.write_bytes(sys.stdin.buffer.read()); print(p)"
    )
    result = checked(base + [shlex.join(["python3", "-c", upload_code])],
                     input=Path(__file__).read_text(), text=True, capture_output=True)
    path = result.stdout.strip()
    if not re.fullmatch(r"/[^\n]+/flutter-termux-[A-Za-z0-9_-]+/worker\.py", path):
        raise BuildError("Réponse inattendue lors du transfert SSH du worker.")
    return base, path


def docker_command(config: dict, args) -> list[str]:
    if not shutil.which("docker"):
        raise BuildError("Docker absent. Installer/démarrer Docker Desktop, Colima ou Docker Engine,"
                         " ou choisir --target ssh. Voir README.md.")
    context = config.get("docker_context") or checked(["docker", "context", "show"],
                                                      text=True, capture_output=True).stdout.strip()
    prefix = ["docker", "--context", context]
    host = json.loads(checked(prefix + ["context", "inspect", context, "--format",
                                       "{{json .Endpoints.docker.Host}}"],
                              text=True, capture_output=True).stdout)
    if not host.startswith("unix://"):
        raise BuildError(f"Le contexte Docker {context!r} pointe vers un moteur distant ({host})."
                         " Pour compiler localement, régler DOCKER_CONTEXT sur un contexte Unix local"
                         " (voir docker context ls), ou choisir --target ssh.")
    checked(prefix + ["info"], stdout=subprocess.DEVNULL)
    host_free = shutil.disk_usage(Path(config["artifact_dir"]).parent if
                                 Path(config["artifact_dir"]).parent.exists() else Path.cwd()).free
    require_space(host_free, 2, "export local des artefacts")
    image = subprocess.run(prefix + ["image", "inspect", config["docker_image"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if image.returncode:
        if config["check"]:
            raise BuildError("Image Docker absente : lancer build pour proposer sa construction.")
        if not confirm("Construire l'image Linux et installer ses dépendances ?",
                       args.yes or config.get("auto_install", False)):
            raise BuildError("Construction de l'image refusée.")
        # L'image peut consommer quelques Gio avant le précontrôle dans le volume.
        require_space(host_free, 8, "stockage hôte pour Docker")
        checked(prefix + ["build", "--platform", "linux/amd64", "-t", config["docker_image"],
                 str(Path(__file__).resolve().parent)])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", config["docker_volume"]):
        raise BuildError("DOCKER_VOLUME invalide.")
    # Volume Linux : évite les problèmes de casse et d'I/O des bind mounts macOS.
    Path(config["artifact_dir"]).mkdir(parents=True, exist_ok=True)
    config["work_dir"] = "/work"
    config["export_dir"] = "/export"
    return prefix + ["run", "--rm", "--init", "-i", "--platform", "linux/amd64",
            "--mount", f"type=volume,src={config['docker_volume']},dst=/work",
            "--mount", f"type=bind,src={Path(__file__).resolve().parent},dst=/tool,readonly",
            "--mount", f"type=bind,src={config['artifact_dir']},dst=/export",
            config["docker_image"], "python3", "-u", "/tool/flutter_termux.py"]


def drive(command: list[str], config: dict, args) -> int:
    payload = base64.b64encode(json.dumps(config).encode()).decode()
    proc = subprocess.Popen(command + ["--worker", payload], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(line, end="", flush=True)
                continue
            kind = event.get("event")
            if kind == "ask":
                license_question = event.get("license", False)
                automatic = ((args.accept_android_licenses or config.get("accept_android_licenses", False))
                             if license_question else (args.yes or config.get("auto_install", False)))
                answer = confirm(event["message"], automatic)
                proc.stdin.write(json.dumps({"answer": answer}) + "\n")
                proc.stdin.flush()
            else:
                print(event.get("message", line.rstrip()), flush=True)
        return proc.wait()
    except (KeyboardInterrupt, BuildError):
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def emit(message: str, event="log", **extra):
    print(json.dumps({"event": event, "message": message, **extra}, ensure_ascii=False), flush=True)


def ask(message: str, license=False) -> bool:
    emit(message, "ask", license=license)
    line = sys.stdin.readline()
    if not line:
        raise BuildError("Contrôleur déconnecté pendant une confirmation.")
    return json.loads(line).get("answer") is True


def ancestor(path: Path) -> Path:
    path = path.resolve()
    return next(x for x in (path, *path.parents) if x.exists())


def require_space(free: int, required_gib: float, label: str):
    if free < required_gib * GIB:
        raise BuildError(f"Espace insuffisant sur {label} : {free / GIB:.1f} Gio libres ;"
                         f" {required_gib:g} Gio exigés. Aucun téléchargement du build lancé.")


def preflight(config: dict) -> dict:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise BuildError("Ce backend exige Linux x86-64. Utiliser --target docker ou --target ssh.")
    work = Path(config["work_dir"]).expanduser().resolve()
    free = shutil.disk_usage(ancestor(work)).free
    require_space(free, config["min_free_gib"], str(work))
    if not os.access(ancestor(work), os.W_OK):
        raise BuildError(f"Dossier non accessible en écriture : {ancestor(work)}")
    # Même contrôle pour un SDK extérieur au workspace.
    for value in (config.get("sdk_root"), config.get("export_dir")):
        if value:
            path = ancestor(Path(value).expanduser())
            if path.stat().st_dev != ancestor(work).stat().st_dev:
                require_space(shutil.disk_usage(path).free, 8, str(path))
    if not shutil.which("dpkg-query"):
        raise BuildError("Installation automatique prévue pour Ubuntu/Debian (dpkg-query requis).")
    missing = []
    for package in PACKAGES:
        probe = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package],
                               capture_output=True, text=True)
        if probe.returncode or probe.stdout.strip() != "install ok installed":
            missing.append(package)
    memory, swap = 0, 0
    if Path("/proc/meminfo").exists():
        match = re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())
        memory = int(match[1]) * 1024 if match else 0
        match = re.search(r"SwapFree:\s+(\d+)", Path("/proc/meminfo").read_text())
        swap = int(match[1]) * 1024 if match else 0
    java = shutil.which("java")
    java_ok = False
    if java:
        result = subprocess.run([java, "-version"], capture_output=True, text=True)
        major = re.search(r'(?:version\s+"|openjdk\s+|java\s+)(\d+)', result.stdout + result.stderr)
        java_ok = result.returncode == 0 and major is not None and int(major[1]) >= 17
    if not java_ok:
        # JDK 17 existe sur Debian 12 ; Ubuntu 24.04 fournit aussi JDK 21.
        missing.append("default-jdk-headless")
    return {"work": str(work), "free": free, "missing": missing, "memory": memory, "swap": swap}


class Runner:
    def __init__(self, config, root: Path):
        self.config = config
        self.root = root
        self.log = None
        self.active = None
        self.env = os.environ.copy()
        self.env.update({"PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0", "CI": "true",
                         "NINJA_STATUS": "[%f/%t %e s] ", "DEPOT_TOOLS_UPDATE": "0",
                         "DEPOT_TOOLS_METRICS": "0"})
        # sdkmanager doit employer le Java validé sur le PATH, pas un ancien JAVA_HOME.
        self.env.pop("JAVA_HOME", None)

    def note(self, message):
        emit(message)
        if self.log:
            self.log.write(message + "\n")
            self.log.flush()

    def disk(self):
        for path in {self.root, Path(self.env.get("ANDROID_SDK_ROOT", str(self.root)))}:
            free = shutil.disk_usage(ancestor(path)).free
            if free < self.config["reserve_gib"] * GIB:
                raise BuildError(f"Réserve disque atteinte sur {path} : {free/GIB:.1f} Gio."
                                 " Build arrêté ; libérer de la place puis utiliser --resume.")

    def run(self, argv, cwd=None, capture=False, input_text=None):
        self.disk()
        self.note("$ " + shlex.join([str(x) for x in argv]))
        child = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=self.env,
                                 stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace", bufsize=1, start_new_session=True)
        self.active = child
        lines = queue.Queue(maxsize=1000)

        def read_output():
            try:
                for line in child.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        if input_text is not None:
            with contextlib.suppress(BrokenPipeError):
                child.stdin.write(input_text)
                child.stdin.close()
        collected = []
        started, last_disk = time.monotonic(), time.monotonic()
        try:
            while True:
                try:
                    line = lines.get(timeout=10)
                except queue.Empty:
                    self.note(f"… en cours ({int(time.monotonic()-started)} s)")
                    line = ""
                if time.monotonic() - last_disk >= 10:
                    self.disk()
                    last_disk = time.monotonic()
                if line is None:
                    break
                if line:
                    if capture:
                        collected.append(line)
                    self.note(line.rstrip("\n"))
            result = child.wait()
            self.disk()
            if result:
                raise BuildError(f"Commande échouée (code {result}) : {shlex.join([str(x) for x in argv])}")
            return "".join(collected)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            child.stdout.close()
            self.active = None


def atomic_json(path: Path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def change_toml(text: str, values: dict[str, dict]) -> str:
    """Modifie seulement les clés demandées, conserve les commentaires et le reste."""
    import tomllib
    original = tomllib.loads(text)
    for section, entries in values.items():
        if section not in original:
            raise BuildError(f"Contrat backend modifié : section [{section}] absente.")
        pattern = re.compile(r"(?ms)^\[" + re.escape(section) + r"\][^\n]*\n(.*?)(?=^\[|\Z)")
        found = pattern.search(text)
        if not found:
            raise BuildError(f"Section TOML non reconnue : {section}")
        body = found[1]
        for key, value in entries.items():
            # Les valeurs utilisées ici sont strings, ints et listes de strings : JSON est du TOML valide.
            replacement = f"{key} = {json.dumps(value)}"
            key_pattern = re.compile(r"(?m)^\s*" + re.escape(key) + r"\s*=.*$")
            if key_pattern.search(body):
                body = key_pattern.sub(lambda _: replacement, body, count=1)
            else:
                body += replacement + "\n"
        text = text[:found.start(1)] + body + text[found.end(1):]
    tomllib.loads(text)
    return text


def backend_methods(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Build"]
    if len(classes) != 1:
        raise BuildError("Contrat backend inconnu : classe Build absente.")
    methods = {n.name: {x.arg for x in n.args.args + n.args.kwonlyargs}
               for n in classes[0].body if isinstance(n, ast.FunctionDef)}
    # Fire expose aussi les objets affectés à self.<commande>, pas seulement les def.
    # Le caractère appelable de ces attributs sera vérifié dans le venv du backend.
    for node in ast.walk(classes[0]):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name) and node.value.id == "self"):
            if node.attr in BACKEND_COMMANDS:
                methods.setdefault(node.attr, set())
    # sysroot peut également être fourni par un descripteur ou une affectation
    # dynamique invisible ici. Ne pas le rejeter avant l'inspection de Build().
    required = set(BACKEND_COMMANDS) - {"sysroot"}
    missing = required - methods.keys()
    if missing:
        raise BuildError("Backend incompatible (méthodes absentes : " + ", ".join(sorted(missing))
                         + "). Choisir un backend récent, sans supprimer les étapes Android AOT.")
    return methods


def runtime_backend_methods(backend: Path, python: Path, runner: Runner) -> dict[str, set[str]]:
    """Inspecte l'interface réelle après installation des imports et configuration NDK.

    Aucun build/sync n'est exécuté : seule la construction de l'objet déjà utilisé
    par Fire et l'inspection de ses commandes ont lieu dans un sous-processus isolé.
    """
    marker = "FLUTTER_TERMUX_COMMANDS="
    code = f'''import inspect, json
from build import Build
backend = Build()
commands = {{}}
missing = []
for name in {BACKEND_COMMANDS!r}:
    command = getattr(backend, name, None)
    if not callable(command):
        missing.append(name)
        continue
    try:
        commands[name] = list(inspect.signature(command).parameters)
    except (ValueError, TypeError):
        commands[name] = []
if missing:
    raise SystemExit("Backend incompatible : commandes absentes ou non appelables : " + ", ".join(missing))
print({marker!r} + json.dumps(commands))
'''
    output = runner.run([python, "-c", code], cwd=backend, capture=True)
    for line in reversed(output.splitlines()):
        if line.startswith(marker):
            methods = json.loads(line[len(marker):])
            if set(methods) == set(BACKEND_COMMANDS) and all(isinstance(x, list) for x in methods.values()):
                return {name: set(parameters) for name, parameters in methods.items()}
            break
    raise BuildError("Interface du backend illisible après inspection ; aucun téléchargement du moteur lancé.")


def android_package(xml: bytes, package: str) -> dict:
    """Sélectionne une archive Linux officielle et son empreinte, sans URL épinglée périmée."""
    root = ET.fromstring(xml)
    # Les préfixes des documents Google peuvent changer.
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    candidates = []
    for remote in root.findall("remotePackage"):
        name = remote.get("path", "")
        if (package == "cmdline-tools;latest" and name.startswith("cmdline-tools;")
                or name == package):
            channel = remote.find("channelRef")
            if channel is not None and channel.get("ref") != "channel-0":
                continue
            revision = remote.find("revision")
            rev = tuple(int(revision.findtext(x, "0")) for x in ("major", "minor", "micro"))
            if revision.find("preview") is not None:
                continue
            for archive in remote.findall("archives/archive"):
                if archive.findtext("host-os") != "linux":
                    continue
                complete = archive.find("complete")
                checksum = complete.find("checksum")
                url = complete.findtext("url", "")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", url):
                    raise BuildError("Nom d'archive Android inattendu.")
                algorithm = checksum.get("type", "sha1").lower().replace("-", "")
                if algorithm not in ("sha1", "sha256"):
                    raise BuildError("Algorithme d'empreinte Android inconnu.")
                candidates.append((rev, {"path": name, "url": ANDROID_REPOSITORY + url,
                                        "checksum": checksum.text.strip(), "algorithm": algorithm}))
    if not candidates:
        raise BuildError(f"Paquet Android officiel introuvable : {package}."
                         " Régler NDK_VERSION sur une version officielle compatible ou ANDROID_NDK"
                         " sur un NDK hôte existant ; aucune substitution silencieuse.")
    return max(candidates, key=lambda x: x[0])[1]


def download_archive(info: dict, destination: Path, runner: Runner):
    partial = destination.with_suffix(".part")
    runner.note("Téléchargement vérifié : " + info["url"])
    digest = hashlib.new(info["algorithm"])
    try:
        with urllib.request.urlopen(info["url"], timeout=60) as response, partial.open("wb") as output:
            last = time.monotonic()
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                total += len(block)
                if time.monotonic() - last > 5:
                    runner.disk()
                    runner.note(f"  {total / GIB:.2f} Gio téléchargés")
                    last = time.monotonic()
        if digest.hexdigest().lower() != info["checksum"].lower():
            raise BuildError("Empreinte de l'archive Android incorrecte.")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def extract_zip(archive: Path, destination: Path):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            path = (destination / entry.filename).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise BuildError("Archive Android contenant un chemin hors du dossier cible.")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise BuildError("Lien symbolique inattendu dans l'archive Android.")
            source.extract(entry, destination)
            mode = (entry.external_attr >> 16) & 0o777
            if mode:
                path.chmod(mode)


def provision_ndk(config: dict, backend: Path, runner: Runner) -> Path:
    import tomllib
    build_config = tomllib.loads((backend / "build.toml").read_text())
    version = config.get("ndk_version") or build_config["ndk"]["version"]
    sdk = Path(config.get("sdk_root") or runner.root / "tools/android-sdk").expanduser().resolve()
    runner.env["ANDROID_SDK_ROOT"] = str(sdk)
    runner.env["ANDROID_HOME"] = str(sdk)
    choices_file = sdk / ".flutter-termux-ndk.json"
    choices = {}
    if choices_file.exists():
        try:
            choices = json.loads(choices_file.read_text())
            if not isinstance(choices, dict):
                choices = {}
        except (ValueError, OSError):
            runner.note("Choix NDK mémorisé illisible ; nouvelle résolution officielle.")
    requested_version = version
    if not config.get("ndk_version") and not config.get("ndk"):
        saved = choices.get(requested_version)
        if (isinstance(saved, str) and re.fullmatch(r"\d+\.\d+\.\d+", saved)
                and saved.split(".")[0] == version.split(".")[0]):
            saved_ndk = sdk / "ndk" / saved
            saved_clang = saved_ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
            if saved_clang.is_file() and os.access(saved_clang, os.X_OK) and (saved_ndk / "source.properties").is_file():
                runner.note(f"Choix NDK déjà validé : {requested_version} → {saved}")
                runner.run([saved_clang, "--version"])
                return saved_ndk
    ndk = Path(config["ndk"]).expanduser().resolve() if config.get("ndk") else sdk / "ndk" / version
    clang = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
    if clang.is_file() and os.access(clang, os.X_OK) and (ndk / "source.properties").exists():
        runner.note("NDK hôte existant : " + str(ndk))
        runner.run([clang, "--version"])
        return ndk
    if config.get("ndk"):
        raise BuildError(f"ANDROID_NDK invalide : {ndk} (NDK Linux x86-64 attendu).")
    # Résoudre avant d'installer quoi que ce soit : révèle les pins de forks non officiels.
    xml = request(REPOSITORY_XML)
    replacement = ""
    try:
        android_package(xml, "ndk;" + version)
    except BuildError:
        # Certains forks épinglent un NDK Android/ARM64 repackagé : son numéro
        # n'est pas nécessairement celui du NDK hôte Linux publié par Google.
        # On propose uniquement une version officielle de même génération.
        if config.get("ndk_version"):
            raise
        major = version.split(".")[0]
        candidates = []
        for node in ET.fromstring(xml).iter():
            name = node.get("path", "")
            if re.fullmatch(r"ndk;" + re.escape(major) + r"\.\d+\.\d+", name):
                with contextlib.suppress(BuildError):
                    android_package(xml, name)
                    candidates.append(name.split(";", 1)[1])
        if not candidates:
            raise
        candidate = max(candidates, key=lambda x: tuple(map(int, x.split("."))))
        replacement = f" (remplace le pin hôte introuvable {version}, même génération {major})"
        runner.note(f"NDK hôte proposé : {candidate}{replacement}")
        version = candidate
        ndk = sdk / "ndk" / version
        clang = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
        if clang.is_file() and os.access(clang, os.X_OK) and (ndk / "source.properties").is_file():
            runner.note("NDK officiel déjà installé, réutilisation : " + str(ndk))
            runner.run([clang, "--version"])
            choices[requested_version] = version
            atomic_json(choices_file, choices)
            return ndk
    sdkmanager = sdk / "cmdline-tools/latest/bin/sdkmanager"
    if not ask(f"Installer le NDK {version}" + (" et Android SDK Command-Line Tools" if not sdkmanager.exists() else "")
               + f" dans {sdk}{replacement} ?"):
        raise BuildError("Installation Android refusée.")
    if not ask("Accepter les licences Android SDK/NDK pour cette installation ?"
               " Conditions : https://developer.android.com/studio/terms", license=True):
        raise BuildError("Licences Android non acceptées.")
    sdk.mkdir(parents=True, exist_ok=True)
    if not sdkmanager.exists():
        info = android_package(xml, "cmdline-tools;latest")
        with tempfile.TemporaryDirectory(dir=sdk) as temp:
            temp = Path(temp)
            archive = temp / "tools.zip"
            download_archive(info, archive, runner)
            extract_zip(archive, temp / "extract")
            source = temp / "extract/cmdline-tools"
            if not (source / "bin/sdkmanager").is_file():
                raise BuildError("Structure des Android Command-Line Tools inattendue.")
            sdkmanager.parent.parent.parent.mkdir(parents=True, exist_ok=True)
            if sdkmanager.parent.parent.exists():
                raise BuildError("Command-Line Tools incomplets : déplacer cmdline-tools/latest puis relancer.")
            source.rename(sdkmanager.parent.parent)
    runner.run([sdkmanager, f"--sdk_root={sdk}", "--licenses"], input_text="y\n" * 200)
    runner.run([sdkmanager, f"--sdk_root={sdk}", "--install", "ndk;" + version], input_text="y\n" * 200)
    if not clang.is_file() or not os.access(clang, os.X_OK):
        raise BuildError("Installation du NDK terminée sans compilateur Linux x86-64.")
    runner.run([clang, "--version"])
    choices[requested_version] = version
    atomic_json(choices_file, choices)
    return ndk


def ninja_limiter(directory: Path, real_ninja: str, jobs: int):
    """Le dernier -j gagne même si un ancien backend impose lui-même -j24."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ninja"
    script.write_text(f"#!{sys.executable}\nimport os, sys\n"
                      f"os.execv({real_ninja!r}, [{real_ninja!r}, *sys.argv[1:], '-j{jobs}'])\n")
    script.chmod(0o755)


def validate_build_outputs(backend: Path, flutter: Path, runner: Runner):
    """Détecte un mode manquant avant le packaging sans inventer ses ressources."""
    missing = []
    for mode in ("release", "debug", "profile"):
        out = flutter / "engine/src/out" / f"linux_{mode}_arm64"
        for name in ("gen_snapshot", "libflutter_linux_gtk.so"):
            if not (out / name).is_file() or (out / name).stat().st_size == 0:
                missing.append(str(out / name))
    if missing:
        raise BuildError("Sorties de compilation requises absentes (aucun alias créé) :\n" + "\n".join(missing))
    runner.note("Sorties Linux release/debug/profile présentes."
                " Le résolveur de debuild contrôlera les ressources supplémentaires de package.yaml.")


def verify_flutter(metadata: dict, release: dict, actual_hash: str):
    if actual_hash != release["hash"] or metadata.get("frameworkRevision") != release["hash"]:
        raise BuildError("Le commit du SDK ne correspond pas à la publication Flutter sélectionnée.")
    if metadata.get("frameworkVersion") != release["version"]:
        raise BuildError("La version du SDK ne correspond pas à la sélection.")
    expected_dart = release.get("dart_sdk_version", "").split(" ")[0]
    if expected_dart and metadata.get("dartSdkVersion", "").split(" ")[0] != expected_dart:
        raise BuildError("Version Dart incohérente avec la publication Flutter.")
    for field in ("frameworkCommitDate", "dartSdkVersion", "devToolsVersion", "engineRevision"):
        if not metadata.get(field):
            raise BuildError(f"Métadonnée Flutter absente : {field}")


def package_inventory(stream) -> dict:
    """Contrôle les outils ARM64 dans le contenu réel du .deb, sans extraction massive."""
    required = {
        "dart": ("bin/cache/dart-sdk/bin/dart",),
        "dartvm": ("bin/cache/dart-sdk/bin/dartvm",),
        "dartaotruntime": ("bin/cache/dart-sdk/bin/dartaotruntime",),
        "dartdev_aot": ("bin/cache/dart-sdk/bin/snapshots/dartdev_aot.dart.snapshot",),
        "android_release_gen_snapshot": ("linux-arm64-android-release/gen_snapshot",
                                         "android-arm64-release/linux-arm64/gen_snapshot"),
        "android_profile_gen_snapshot": ("linux-arm64-android-profile/gen_snapshot",
                                         "android-arm64-profile/linux-arm64/gen_snapshot"),
    }
    found = {}
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            for key, suffixes in required.items():
                if any(member.name.endswith("/" + suffix) for suffix in suffixes):
                    if not member.isfile() or member.size == 0:
                        raise BuildError(f"Ressource requise vide ou indirecte : {member.name}")
                    content = archive.extractfile(member)
                    header = content.read(64)
                    if key != "dartdev_aot":
                        if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 183:
                            raise BuildError(f"Ressource attendue ELF ARM64, format incorrect : {member.name}")
                    found[key] = member.name
    missing = required.keys() - found.keys()
    if missing:
        raise BuildError("Paquet incomplet : " + ", ".join(sorted(missing))
                         + ". Les outils Android AOT ne peuvent pas être remplacés par ceux de Linux.")
    return found


def collect_artifacts(backend: Path, destination: Path, version: str, runner: Runner) -> list[dict]:
    packages = sorted((backend / "release").glob("*.deb")) + sorted(backend.glob("*.deb"))
    if not packages:
        raise BuildError("Aucun .deb produit par le backend ; packaging non validé.")
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for package in packages:
        runner.disk()
        runner.note("Validation du contenu ARM64 : " + package.name)
        fields = runner.run(["dpkg-deb", "-f", package, "Package", "Version", "Architecture"], capture=True)
        if not re.search(r"(?m)^(?:Architecture: )?aarch64$", fields):
            raise BuildError("Architecture inattendue dans " + package.name)
        if not re.search(r"(?m)^(?:Version: )?" + re.escape(version) + r"(?:[-+~].*)?$", fields):
            raise BuildError("Version du paquet incohérente dans " + package.name)
        # Pas de gros stdout encodé JSON : lire directement le flux de l'archive.
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(package)],
                                    stdout=subprocess.PIPE, stderr=errors)
            try:
                resources = package_inventory(proc.stdout)
                if proc.wait():
                    errors.seek(0)
                    raise BuildError("Archive .deb illisible : " + errors.read().decode(errors="replace"))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait()
                proc.stdout.close()
        target = destination / package.name
        partial = destination / (package.name + ".part")
        shutil.copy2(package, partial)
        digest = hashlib.sha256()
        with partial.open("rb") as content:
            for block in iter(lambda: content.read(1024 * 1024), b""):
                digest.update(block)
        partial.replace(target)
        checksum = digest.hexdigest()
        target.with_suffix(target.suffix + ".sha256").write_text(f"{checksum}  {target.name}\n")
        inventory.append({"file": target.name, "size": target.stat().st_size, "sha256": checksum,
                          "validated_resources": resources})
    return inventory


def execute_build(config: dict, runner: Runner, workspace: Path):
    release = config["release"]
    backend = workspace / "backend"
    state_file = workspace / "state.json"
    identity = {k: config.get(k) for k in ("backend_repo", "backend_ref", "ndk", "ndk_version", "sdk_root")}
    identity["release"] = release
    identity["recipe_version"] = 1
    state = {"identity": identity, "completed": []}
    if state_file.exists():
        old = json.loads(state_file.read_text())
        if old["identity"] != identity:
            raise BuildError("Configuration différente de ce workspace. Choisir un autre WORK_DIR.")
        if not config["resume"]:
            raise BuildError("Build existant : utiliser --resume pour reprendre ce workspace.")
        state = old
    if config.get("rebuild_from"):
        first = config["rebuild_from"]
        state["completed"] = [x for x in state["completed"] if int(x.split("/", 1)[0]) < first]
    atomic_json(state_file, state)

    def stage(name, action):
        if name in state["completed"]:
            runner.note(f"[reprise] {name} déjà terminé")
            return
        runner.note(f"\n▶ {name}")
        state["current"] = name
        atomic_json(state_file, state)
        action()
        state["completed"].append(name)
        atomic_json(state_file, state)
        runner.note(f"✓ {name}")

    def checkout_backend():
        if not backend.exists():
            runner.run(["git", "clone", "--no-checkout", "--filter=blob:none", "--",
                        config["backend_repo"], backend])
        if not (backend / ".git").is_dir():
            raise BuildError("Le dossier backend existe sans dépôt Git.")
        # Une interruption après fetch ne change pas la référence retenue.
        if not state.get("backend_commit"):
            runner.run(["git", "fetch", "--depth=1", "origin", config["backend_ref"]], cwd=backend)
            state["backend_commit"] = runner.run(["git", "rev-parse", "FETCH_HEAD"], cwd=backend, capture=True).strip()
            atomic_json(state_file, state)
        runner.run(["git", "checkout", "--detach", state["backend_commit"]], cwd=backend)

    stage("1/10 — dépôt des adaptations Termux", checkout_backend)
    actual_backend = runner.run(["git", "rev-parse", "HEAD"], cwd=backend, capture=True).strip()
    if actual_backend != state.get("backend_commit"):
        raise BuildError("Le commit du backend a changé depuis le début de ce build.")
    methods = backend_methods(backend / "build.py")
    config_file = backend / "build.toml"

    def configure_version():
        values = {"flutter": {"repo": "https://github.com/flutter/flutter.git", "path": "./flutter",
                               "tag": release["version"], "framework_revision": release["hash"],
                               "dart_version": release.get("dart_sdk_version", "").split(" ")[0]},
                  "build": {"arch": ["arm64"], "runtime": ["release", "debug", "profile"],
                            "android_runtime": ["release", "profile"]}}
        config_file.write_text(change_toml(config_file.read_text(), values))

    stage("2/10 — sélection de la version officielle", configure_version)
    venv = workspace / "venv"
    python = venv / "bin/python3"

    def dependencies():
        runner.run([sys.executable, "-m", "venv", venv])
        runner.run([python, "-m", "pip", "install", "-r", backend / "requirements.txt"])

    stage("3/10 — dépendances Python isolées", dependencies)
    depot = workspace / "depot_tools"
    def depot_tools():
        depot.parent.mkdir(parents=True, exist_ok=True)
        if not depot.exists():
            runner.run(["git", "clone", "--depth=1", "--",
                        "https://chromium.googlesource.com/chromium/tools/depot_tools.git", depot])
        commit = runner.run(["git", "rev-parse", "HEAD"], cwd=depot, capture=True).strip()
        if state.get("depot_tools_commit") and state["depot_tools_commit"] != commit:
            raise BuildError("Le commit depot_tools a changé pendant la reprise.")
        state["depot_tools_commit"] = commit
        atomic_json(state_file, state)
    # Une version nouvelle démarre avec depot_tools récent, épinglé pendant ses reprises.
    depot_tools()
    real_ninja = shutil.which("ninja")
    if not real_ninja:
        raise BuildError("ninja absent après installation des dépendances.")
    limiter = workspace / "limited-tools"
    ninja_limiter(limiter, real_ninja, config["jobs"])
    runner.env["PATH"] = os.pathsep.join([str(limiter), str(venv / "bin"), str(depot), runner.env["PATH"]])
    runner.note(f"Ninja : {config['jobs']} job(s) maximum via PATH et option du backend si disponible.")
    # Le NDK est revérifié à chaque reprise ; sa présence n'est pas déduite d'un marqueur.
    runner.note("\n▶ Vérification Android SDK / NDK hôte")
    ndk = provision_ndk(config, backend, runner)
    runner.env["ANDROID_NDK"] = str(ndk)
    for alias in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_PATH"):
        runner.env[alias] = str(ndk)
    props = (ndk / "source.properties").read_text()
    revision = re.search(r"(?m)^Pkg.Revision\s*=\s*(\S+)", props)
    if not revision:
        raise BuildError("Pkg.Revision absent du NDK hôte.")
    config_file.write_text(change_toml(config_file.read_text(), {"ndk": {"path": str(ndk), "version": revision[1]}}))
    runner.note("\n▶ Vérification des commandes réelles du backend (méthodes et objets appelables)")
    runtime_methods = runtime_backend_methods(backend, python, runner)
    for name, parameters in runtime_methods.items():
        methods.setdefault(name, set()).update(parameters)

    def command(method, *flags):
        options = list(flags)
        if "jobs" in methods.get(method, ()):
            options.append(f"--jobs={config['jobs']}")
        runner.run([python, "build.py", method, *options], cwd=backend)

    stage("4/10 — sources officielles Flutter", lambda: command("clone"))
    import tomllib
    flutter_path = tomllib.loads(config_file.read_text())["flutter"].get("path", "./flutter")
    flutter = (backend / flutter_path).resolve()
    if not flutter.is_relative_to(backend.resolve()):
        raise BuildError("Chemin Flutter hors du dépôt backend.")
    actual_hash = runner.run(["git", "rev-parse", "HEAD"], cwd=flutter, capture=True).strip()

    def pin_metadata():
        # Flutter peut émettre des messages de bootstrap avant son objet JSON.
        output = runner.run([flutter / "bin/flutter", "--version", "--machine"], cwd=flutter, capture=True)
        start = output.find("{\n")
        if start < 0:
            start = output.find("{")
        try:
            metadata, _ = json.JSONDecoder().raw_decode(output[start:])
        except (ValueError, TypeError):
            raise BuildError("Métadonnées --version --machine illisibles.") from None
        verify_flutter(metadata, release, actual_hash)
        values = {"flutter": {"tag": release["version"], "framework_revision": release["hash"],
                               "framework_commit_date": metadata["frameworkCommitDate"],
                               "dart_version": metadata["dartSdkVersion"].split(" ")[0],
                               "devtools_version": metadata["devToolsVersion"]}}
        config_file.write_text(change_toml(config_file.read_text(), values))
        atomic_json(workspace / "flutter-metadata.json", metadata)

    stage("5/10 — vérification et synchronisation des métadonnées", pin_metadata)
    verify_flutter(json.loads((workspace / "flutter-metadata.json").read_text()), release, actual_hash)
    stage("6/10 — moteur, dépendances et correctifs Bionic", lambda: command("sync"))
    stage("7/10 — sysroot Termux ARM64", lambda: command("sysroot", "--arch=arm64"))

    def desktop():
        for mode in ("release", "debug", "profile"):
            command("configure", "--arch=arm64", f"--mode={mode}")
            command("build", "--arch=arm64", f"--mode={mode}")

    stage("8/10 — Dart SDK et moteurs Linux (release, debug, profile)", desktop)

    def android():
        for mode in ("release", "profile"):
            command("configure_android", "--arch=arm64", f"--mode={mode}")
            command("build_android", "--arch=arm64", f"--mode={mode}")

    stage("9/10 — outils AOT ciblant Android (release + profile)", android)
    def package():
        validate_build_outputs(backend, flutter, runner)
        command("debuild", "--arch=arm64")
    stage("10/10 — paquet Debian Termux", package)
    output = Path(config.get("export_dir") or workspace / "artifacts") / release["version"]
    inventory = collect_artifacts(backend, output, release["version"], runner)
    evidence = {"flutter": release, "backend_repo": config["backend_repo"],
                "backend_commit": actual_backend, "depot_tools_commit": state["depot_tools_commit"],
                "ndk": str(ndk), "jobs": config["jobs"], "artifacts": inventory,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "termux_device_test": "not_run"}
    atomic_json(output / "build-evidence.json", evidence)
    state["current"] = "complete"
    atomic_json(state_file, state)
    runner.note("\nBuild terminé. Artefacts : " + str(output))
    runner.note("Validation sur appareil Termux encore nécessaire : voir README.md.")


def install_signal_handlers():
    def interrupted(signum, frame):
        raise BuildError(f"Worker interrompu (signal {signum}).")
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        signal.signal(getattr(signal, name), interrupted)


def worker(config: dict) -> int:
    import fcntl
    install_signal_handlers()
    runner = None
    try:
        report = preflight(config)
        emit(f"Cible Linux x86-64 : {report['free']/GIB:.1f} Gio libres,"
             f" {os.cpu_count()} CPU, {report['memory']/GIB:.1f} Gio RAM disponible,"
             f" {report['swap']/GIB:.1f} Gio swap libre.")
        if report["memory"] < 8 * GIB:
            emit("RAM disponible inférieure à 8 Gio : conserver JOBS=1 ; le linker peut nécessiter du swap.")
        emit("Dépendances système manquantes : " + (", ".join(report["missing"]) or "aucune"))
        if config["check"]:
            emit("Précontrôle terminé sans installation. Le SDK/NDK exact sera vérifié avec le backend au build.")
            return 2 if report["missing"] else 0
        root = Path(report["work"])
        root.mkdir(parents=True, exist_ok=True)
        # Un verrou global évite plusieurs compilations qui épuiseraient le même disque.
        with (root / ".build.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BuildError("Un autre build utilise ce WORK_DIR.") from None
            release = config["release"]
            workspace = root / "builds" / release["version"]
            workspace.mkdir(parents=True, exist_ok=True)
            runner = Runner(config, root)
            with (workspace / "build.log").open("a", buffering=1) as log:
                runner.log = log
                runner.note("Journal distant/local : " + str(workspace / "build.log"))
                if report["missing"]:
                    if not ask("Installer les dépendances manquantes avec apt-get : "
                               + ", ".join(report["missing"]) + " ?"):
                        raise BuildError("Installation des dépendances refusée.")
                    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
                    runner.run(sudo + ["apt-get", "update"])
                    runner.run(sudo + ["apt-get", "install", "-y", "--no-install-recommends", *report["missing"]])
                    remaining = preflight(config)["missing"]
                    if remaining:
                        raise BuildError("Dépendances encore manquantes : " + ", ".join(remaining))
                execute_build(config, runner, workspace)
        return 0
    except (BuildError, OSError, ValueError, KeyError, urllib.error.URLError, SyntaxError,
            tarfile.TarError, zipfile.BadZipFile) as error:
        emit("ERREUR : " + str(error), "error")
        return 1


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("build", "check", "versions"), nargs="?", default="build")
    p.add_argument("--env", default=".env", help="fichier de configuration (défaut : .env)")
    p.add_argument("--target", choices=("local", "ssh", "docker"))
    p.add_argument("--channel", choices=("stable", "beta"), default="stable")
    p.add_argument("--count", type=int, default=5, help="nombre de versions proposées (défaut : 5)")
    p.add_argument("--version", help="version officielle exacte, sinon menu avec la dernière par défaut")
    p.add_argument("--work-dir")
    p.add_argument("--min-free-gib", type=float)
    p.add_argument("--resume", action="store_true", help="reprendre les étapes terminées du même build")
    p.add_argument("--rebuild-from", type=int, choices=range(1, 11), metavar="1..10",
                   help="avec --resume : rejouer à partir de cette étape (8 = compilation des moteurs)")
    p.add_argument("--jobs", type=int, help="parallélisme Ninja (défaut : 1, également limité par wrapper PATH)")
    p.add_argument("--yes", action="store_true", help="valider la version et les installations (hors licences Android)")
    p.add_argument("--auto-install", action="store_true",
                   help="installer les prérequis automatiquement en conservant le menu des versions")
    p.add_argument("--accept-android-licenses", action="store_true", help="accepter explicitement les licences Android")
    p.add_argument("--releases-file", help="manifest officiel sauvegardé (tests ou réseau restreint)")
    p.add_argument("--worker", help=argparse.SUPPRESS)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.version_info < (3, 11):
        raise BuildError("Python 3.11+ requis.")
    if args.worker:
        return worker(json.loads(base64.b64decode(args.worker)))
    if not 1 <= args.count <= 30:
        raise BuildError("--count doit être compris entre 1 et 30.")
    config = settings(args)
    if args.rebuild_from and not args.resume:
        raise BuildError("--rebuild-from nécessite --resume.")
    config["rebuild_from"] = args.rebuild_from
    if args.command != "check":
        config["release"] = select_release(args)
        if args.command == "versions":
            return 0
        selected = config["release"]
        print(f"\nFlutter {selected['version']} / {selected['hash'][:12]} — cible {config['target']}")
        print("Le backend Termux et ses correctifs seront testés avec cette version officielle.")
        if not confirm("Lancer le contrôle des prérequis puis la compilation ?", args.yes):
            return 0
    if config["target"] == "ssh":
        base, remote_path = remote_worker(config)
        # Une seule chaîne distante échappée ; aucun champ .env n'est interprété comme du shell.
        command = base + [shlex.join(["python3", "-u", remote_path])]
        # SSH concatène ses arguments avec des espaces ; payload base64 sans métacaractères.
        try:
            return drive(command, config, args)
        finally:
            cleanup = "import shutil,sys; from pathlib import Path; shutil.rmtree(Path(sys.argv[1]).parent)"
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(base + [shlex.join(["python3", "-c", cleanup, remote_path])],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if config["target"] == "docker":
        return drive(docker_command(config, args), config, args)
    return drive([sys.executable, "-u", str(Path(__file__).resolve())], config, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print("ERREUR : " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, urllib.error.URLError) as error:
        print("ERREUR : " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nInterrompu. Consulter build.log et relancer avec --resume après arrêt du worker.", file=sys.stderr)
        raise SystemExit(130)
