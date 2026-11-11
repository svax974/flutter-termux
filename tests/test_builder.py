# SPDX-License-Identifier: MIT
# Copyright (c) 2026 svax974

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import flutter_termux as b


HASH = "a" * 40
BACKEND_HASH = "b" * 40
DEPOT_HASH = "c" * 40
RELEASE = {"channel": "stable", "version": "9.1.0", "hash": HASH,
           "dart_sdk_arch": "x64", "dart_sdk_version": "9.2.0",
           "release_date": "2099-01-01T00:00:00Z"}
METADATA = {"frameworkVersion": "9.1.0", "frameworkRevision": HASH,
            "dartSdkVersion": "9.2.0", "engineRevision": "d" * 40,
            "frameworkCommitDate": "2099-01-01", "devToolsVersion": "9.3.0"}
TOML = """[flutter]
tag = 'old'
repo = 'https://example.invalid/flutter'
path = './flutter'
dart_version = 'old'
framework_revision = 'old'
devtools_version = 'old'
[ndk]
version = '29.0.1'
api = 26
[build]
runtime = ['release']
arch = ['arm64']
[package]
path = 'release'
"""
METHODS = "\n".join(f"    def {method}(self, arch=None, mode=None, jobs=None): pass"
                     for method in ("clone", "sync", "sysroot", "configure", "build",
                                    "configure_android", "build_android", "debuild"))


class CoreTests(unittest.TestCase):
    def test_menu_offers_five_versions_and_manual_entry_for_an_older_release(self):
        args = b.parser().parse_args(["build"])
        rows = [dict(RELEASE, version=f"9.1.{i}", hash=f"{i + 1:040x}",
                     release_date=f"2099-01-{i + 1:02d}T00:00:00Z") for i in range(7)]
        doc = {"current_release": {"stable": rows[-1]["hash"]}, "releases": rows}
        with patch.object(b, "request", return_value=json.dumps(doc).encode()), \
             patch.object(b.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", side_effect=["6", "9.1.0"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            selected = b.select_release(args)
        self.assertEqual(selected["version"], "9.1.0")
        self.assertEqual(output.getvalue().count(". Flutter "), 5)
        self.assertIn("6. Saisir une autre version", output.getvalue())

    def test_manual_version_is_checked_against_the_official_catalogue(self):
        args = b.parser().parse_args(["build"])
        doc = {"current_release": {"stable": HASH}, "releases": [RELEASE]}
        with patch.object(b, "request", return_value=json.dumps(doc).encode()), \
             patch.object(b.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", side_effect=["2", "99.99.99"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(b.BuildError, "99.99.99 n'est pas publié"):
                b.select_release(args)

    def test_versions_fetches_the_official_catalogue_path(self):
        args = b.parser().parse_args(["versions"])
        document = {"current_release": {"stable": HASH}, "releases": [RELEASE]}
        with patch.object(b.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(document).encode())) as urlopen, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            selected = b.select_release(args)
        self.assertEqual(urlopen.call_args.args[0].full_url,
                         "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json")
        self.assertEqual(selected["hash"], HASH)
        self.assertIn("Flutter 9.1.0", output.getvalue())

    def test_http_errors_identify_the_requested_url(self):
        error = b.urllib.error.HTTPError(b.RELEASES_URL, 404, "Not Found", {}, None)
        with patch.object(b.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(b.BuildError) as caught:
                b.request(b.RELEASES_URL)
        self.assertIn(b.RELEASES_URL, str(caught.exception))
        self.assertIn("HTTP 404", str(caught.exception))

    def test_latest_is_official_current_and_architectures_are_deduplicated(self):
        older = dict(RELEASE, version="9.0.0", hash="e" * 40)
        duplicate = dict(RELEASE, dart_sdk_arch="arm64")
        misleading_date = dict(older, release_date="9999-01-01")
        doc = {"current_release": {"stable": HASH},
               "releases": [misleading_date, duplicate, RELEASE, dict(RELEASE, channel="beta")]}
        self.assertEqual([x["version"] for x in b.releases(doc, "stable")], ["9.1.0", "9.0.0"])

    def test_env_is_data_never_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nWORK_DIR='~/a space'\nSSH_HOST=$(touch /tmp/danger)\nJOBS=1 # comment\n")
            env = b.read_env(path)
            self.assertEqual(env["WORK_DIR"], "~/a space")
            self.assertEqual(env["SSH_HOST"], "$(touch /tmp/danger)")
            self.assertEqual(env["JOBS"], "1")
            with self.assertRaises(b.BuildError):
                b.ssh_base({"ssh_host": env["SSH_HOST"], "ssh_user": "", "ssh_port": "22"})

    def test_toml_update_preserves_unrelated_sections_and_release_first(self):
        result = b.change_toml(TOML, {"flutter": {"tag": '9.1.0"quoted'},
                                     "build": {"runtime": ["release", "debug", "profile"]}})
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["flutter"]["tag"], '9.1.0"quoted')
        self.assertEqual(parsed["ndk"]["api"], 26)
        self.assertEqual(parsed["build"]["runtime"], ["release", "debug", "profile"])

    def test_disk_failure_happens_before_any_install_or_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "not-created"
            config = {"work_dir": str(work), "min_free_gib": 150}
            with patch.object(b.platform, "system", return_value="Linux"), \
                 patch.object(b.platform, "machine", return_value="x86_64"), \
                 patch.object(b.shutil, "disk_usage", return_value=type("Disk", (), {"free": b.GIB})()), \
                 patch.object(b.subprocess, "run") as run:
                with self.assertRaisesRegex(b.BuildError, "Espace insuffisant"):
                    b.preflight(config)
                run.assert_not_called()
                self.assertFalse(work.exists())

    def test_remote_disk_failure_prevents_worker_upload(self):
        config = {"ssh_host": "builder", "ssh_user": "", "ssh_port": "22", "ssh_identity": "",
                  "work_dir": "~/with spaces;literal", "min_free_gib": 150}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps({"python": [3, 12], "free": b.GIB}))
        with patch.object(b.subprocess, "run", return_value=response) as run:
            with self.assertRaisesRegex(b.BuildError, "Espace insuffisant"):
                b.remote_worker(config)
            self.assertEqual(run.call_count, 1)
            command = shlex.split(run.call_args.args[0][-1])
            self.assertEqual(command[-1], config["work_dir"])

    def test_docker_local_mode_rejects_a_remote_engine_before_contacting_it(self):
        config = {"docker_context": "remote"}
        response = subprocess.CompletedProcess([], 0, stdout='"ssh://builder.example.net"\n')
        with patch.object(b.shutil, "which", return_value="/bin/docker"), \
             patch.object(b, "checked", return_value=response) as checked:
            with self.assertRaisesRegex(b.BuildError, "moteur distant"):
                b.docker_command(config, None)
            self.assertEqual(checked.call_count, 1)
            self.assertIn("inspect", checked.call_args.args[0])

    def test_metadata_hash_and_dart_must_match(self):
        b.verify_flutter(METADATA, RELEASE, HASH)
        with self.assertRaises(b.BuildError):
            b.verify_flutter(dict(METADATA, dartSdkVersion="9.0.0"), RELEASE, HASH)
        with self.assertRaises(b.BuildError):
            b.verify_flutter(METADATA, RELEASE, "0" * 40)

    def test_old_backend_cannot_skip_android_aot(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "build.py"
            script.write_text("class Build:\n" + METHODS)
            self.assertIn("build_android", b.backend_methods(script))
            script.write_text("class Build:\n    def clone(self): pass\n")
            with self.assertRaisesRegex(b.BuildError, "Backend incompatible"):
                b.backend_methods(script)

    def test_backend_accepts_a_callable_sysroot_attribute_and_inspects_its_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "build.py"
            methods = "\n".join(line for line in METHODS.splitlines() if "def sysroot(" not in line)
            script.write_text("class Sysroot:\n    def __call__(self, arch='arm64'): pass\n"
                              "class Build:\n    def __init__(self):\n        self.sysroot = Sysroot()\n"
                              + methods + "\n")
            self.assertIn("sysroot", b.backend_methods(script))
            runner = b.Runner({"reserve_gib": 0.000001}, root)
            with contextlib.redirect_stdout(io.StringIO()):
                commands = b.runtime_backend_methods(root, Path(sys.executable), runner)
            self.assertEqual(commands["sysroot"], {"arch"})
            self.assertIn("jobs", commands["build"])

    def test_runtime_check_rejects_a_non_callable_sysroot_before_compilation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            methods = "\n".join(line for line in METHODS.splitlines() if "def sysroot(" not in line)
            (root / "build.py").write_text("class Build:\n    def __init__(self):\n        self.sysroot = '/a/path'\n"
                                           + methods + "\n")
            b.backend_methods(root / "build.py")
            runner = b.Runner({"reserve_gib": 0.000001}, root)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(b.BuildError):
                    b.runtime_backend_methods(root, Path(sys.executable), runner)
            self.assertIn("commandes absentes ou non appelables : sysroot", output.getvalue())

    def test_ninja_wrapper_appends_actual_limit_even_after_backend_j24(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ninja = root / "real-ninja"
            ninja.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            ninja.chmod(0o755)
            b.ninja_limiter(root / "limit", str(ninja), 1)
            output = subprocess.check_output([root / "limit/ninja", "-C", "out", "-j24"], text=True)
            self.assertEqual(json.loads(output), ["-C", "out", "-j24", "-j1"])

    def test_android_archives_are_stable_linux_and_have_official_checksum(self):
        xml = b'''<repository><remotePackage path="cmdline-tools;19.0">
        <revision><major>19</major></revision><channelRef ref="channel-0"/>
        <archives><archive><host-os>linux</host-os><complete>
        <checksum type="sha-256">abc</checksum><url>commandlinetools-linux-1_latest.zip</url>
        </complete></archive></archives></remotePackage>
        <remotePackage path="cmdline-tools;20.0"><revision><major>20</major><preview>1</preview></revision>
        <channelRef ref="channel-1"/></remotePackage></repository>'''
        info = b.android_package(xml, "cmdline-tools;latest")
        self.assertEqual(info["path"], "cmdline-tools;19.0")
        self.assertEqual(info["algorithm"], "sha256")
        with self.assertRaisesRegex(b.BuildError, "introuvable"):
            b.android_package(xml, "ndk;not-published")

    def test_zip_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "evil.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("../escape", "no")
            with self.assertRaises(b.BuildError):
                b.extract_zip(archive, Path(tmp) / "target")
            self.assertFalse((Path(tmp) / "escape").exists())

    def test_checksum_failure_does_not_leave_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tools.zip"
            runner = type("R", (), {"note": lambda *a: None})()
            with patch.object(b.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaisesRegex(b.BuildError, "Empreinte"):
                    b.download_archive({"url": "https://example.invalid", "algorithm": "sha256",
                                        "checksum": "0" * 64}, target, runner)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".part").exists())

    def test_worker_prompts_roundtrip_and_child_exit_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-worker.py"
            script.write_text('''import json,sys
print(json.dumps({"event":"ask", "message":"Installer ?"}), flush=True)
answer=json.loads(sys.stdin.readline())["answer"]
print(json.dumps({"event":"log", "message":"accepted="+str(answer)}), flush=True)
sys.exit(7)
''')
            args = type("Args", (), {"yes": True, "accept_android_licenses": False})()
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                result = b.drive([sys.executable, str(script)], {}, args)
            self.assertEqual(result, 7)
            self.assertIn("accepted=True", stream.getvalue())

    def test_env_can_automate_installation_without_skipping_the_version_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("AUTO_INSTALL=true\nACCEPT_ANDROID_LICENSES=true\n")
            args = b.parser().parse_args(["build", "--env", str(env), "--target", "docker"])
            with patch.dict(os.environ, {"AUTO_INSTALL": "true", "ACCEPT_ANDROID_LICENSES": "true"}):
                config = b.settings(args)
            self.assertTrue(config["auto_install"])
            self.assertTrue(config["accept_android_licenses"])
            self.assertFalse(args.yes)

    def test_worker_installation_and_license_questions_are_automatic_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-worker.py"
            script.write_text('''import json,sys
for license in (False, True):
    print(json.dumps({"event":"ask", "message":"Installer / licences ?", "license":license}), flush=True)
    if not json.loads(sys.stdin.readline())["answer"]:
        sys.exit(9)
sys.exit(0)
''')
            args = b.parser().parse_args(["build"])
            config = {"auto_install": True, "accept_android_licenses": True}
            with patch("builtins.input", side_effect=AssertionError("Aucune question attendue")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.drive([sys.executable, str(script)], config, args), 0)

    def test_runner_preserves_failure_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = b.Runner({"reserve_gib": 0.000001}, Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(b.BuildError, "code 17"):
                    runner.run([sys.executable, "-c", "import sys; print('error'); sys.exit(17)"])

    def test_interrupt_terminates_compiler_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "child.pid"
            child_code = "import os,time; from pathlib import Path; " + \
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
            worker = root / "worker.py"
            worker.write_text("import sys\nfrom pathlib import Path\nimport flutter_termux as b\n"
                              "b.install_signal_handlers()\ntry:\n"
                              f"    b.Runner({{'reserve_gib': 0.000001}}, Path({tmp!r})).run("
                              f"[{sys.executable!r}, '-c', {child_code!r}])\n"
                              "except b.BuildError:\n    sys.exit(1)\n")
            env = dict(os.environ, PYTHONPATH=str(Path(b.__file__).parent))
            proc = subprocess.Popen([sys.executable, str(worker)], env=env, stdout=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text())
                proc.terminate()
                self.assertEqual(proc.wait(timeout=7), 1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def elf_header(machine=183):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def package_tar(omit=(), machine=183):
    stream = io.BytesIO()
    root = "./data/data/com.termux/files/usr/opt/flutter/bin/cache/"
    names = ["dart-sdk/bin/dart", "dart-sdk/bin/dartvm", "dart-sdk/bin/dartaotruntime",
             "dart-sdk/bin/snapshots/dartdev_aot.dart.snapshot",
             "artifacts/engine/linux-arm64-android-release/gen_snapshot",
             "artifacts/engine/linux-arm64-android-profile/gen_snapshot"]
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in names:
            if any(value in name for value in omit):
                continue
            content = b"snapshot-data" if name.endswith(".dart.snapshot") else elf_header(machine)
            member = tarfile.TarInfo(root + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    stream.seek(0)
    return stream


class PackageTests(unittest.TestCase):
    def test_real_archive_layout_is_inspected(self):
        inventory = b.package_inventory(package_tar())
        self.assertEqual(len(inventory), 6)

    def test_missing_android_profile_tool_fails_even_if_other_tools_exist(self):
        with self.assertRaisesRegex(b.BuildError, "android_profile_gen_snapshot"):
            b.package_inventory(package_tar(omit=("android-profile",)))

    def test_x86_host_binary_cannot_be_packaged_as_arm64(self):
        with self.assertRaisesRegex(b.BuildError, "ELF ARM64"):
            b.package_inventory(package_tar(machine=62))


class NdkTests(unittest.TestCase):
    def test_ndk_selection_is_combined_with_installation_and_remembered_between_builds(self):
        xml = b'''<repository><remotePackage path="ndk;29.0.2">
        <revision><major>29</major><minor>0</minor><micro>2</micro></revision>
        <channelRef ref="channel-0"/><archives><archive><host-os>linux</host-os><complete>
        <checksum type="sha-256">abc</checksum><url>android-ndk-r29-linux.zip</url>
        </complete></archive></archives></remotePackage></repository>'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            backend = root / "backend"
            backend.mkdir()
            (backend / "build.toml").write_text(TOML)
            sdk = root / "tools/android-sdk"
            sdkmanager = sdk / "cmdline-tools/latest/bin/sdkmanager"
            sdkmanager.parent.mkdir(parents=True)
            sdkmanager.touch()
            ndk = sdk / "ndk/29.0.2"
            commands = []

            class NdkRunner:
                def __init__(self):
                    self.root = root
                    self.env = {}
                def note(self, message):
                    pass
                def run(self, argv, **kwargs):
                    commands.append([str(x) for x in argv])
                    if "--install" in argv:
                        clang = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
                        clang.parent.mkdir(parents=True)
                        clang.touch()
                        clang.chmod(0o755)
                        (ndk / "source.properties").write_text("Pkg.Revision = 29.0.2\n")

            runner = NdkRunner()
            with patch.object(b, "request", return_value=xml), patch.object(b, "ask", side_effect=[True, True]) as ask:
                self.assertEqual(b.provision_ndk({}, backend, runner), ndk)
                self.assertEqual(ask.call_count, 2)
                self.assertIn("remplace le pin", ask.call_args_list[0].args[0])
            saved = json.loads((sdk / ".flutter-termux-ndk.json").read_text())
            self.assertEqual(saved, {"29.0.1": "29.0.2"})
            previous = len(commands)
            with patch.object(b, "request", side_effect=AssertionError("Pas de réseau pour un choix déjà installé")), \
                 patch.object(b, "ask", side_effect=AssertionError("Pas de nouvelle confirmation")):
                self.assertEqual(b.provision_ndk({}, backend, runner), ndk)
            self.assertEqual(len(commands), previous + 1)
            with patch.object(b, "request", return_value=xml), patch.object(b, "ask") as ask:
                with self.assertRaisesRegex(b.BuildError, "introuvable"):
                    b.provision_ndk({"ndk_version": "29.0.1"}, backend, runner)
                ask.assert_not_called()


class FakeRunner(b.Runner):
    def __init__(self, config, root):
        super().__init__(config, root)
        self.commands = []
        self.omit_profile = False

    def note(self, message):
        pass

    def run(self, argv, cwd=None, capture=False, input_text=None):
        args = [str(x) for x in argv]
        self.commands.append(args)
        if args[:2] == ["git", "clone"]:
            path = Path(args[-1])
            (path / ".git").mkdir(parents=True)
            if path.name == "backend":
                (path / "build.py").write_text("class Build:\n" + METHODS)
                (path / "build.toml").write_text(TOML)
                (path / "requirements.txt").write_text("")
        if args[:2] == ["git", "rev-parse"]:
            return ({"backend": BACKEND_HASH, "depot_tools": DEPOT_HASH}.get(Path(cwd).name, HASH)) + "\n"
        if "-m" in args and "venv" in args:
            path = Path(args[-1]) / "bin"
            path.mkdir(parents=True, exist_ok=True)
            (path / "python3").touch()
        if len(args) > 2 and args[1] == "build.py":
            if args[2] == "clone":
                (Path(cwd) / "flutter").mkdir(exist_ok=True)
            if args[2] == "build":
                mode = next(x.split("=", 1)[1] for x in args if x.startswith("--mode="))
                if mode != "profile" or not self.omit_profile:
                    out = Path(cwd) / "flutter/engine/src/out" / f"linux_{mode}_arm64"
                    out.mkdir(parents=True, exist_ok=True)
                    for name in ("gen_snapshot", "libflutter_linux_gtk.so"):
                        (out / name).write_bytes(elf_header())
        if args[-2:] == ["--version", "--machine"]:
            return "Downloading tools...\n" + json.dumps(METADATA, indent=2) + "\n"
        if "-c" in args and "FLUTTER_TERMUX_COMMANDS=" in args[-1]:
            return "FLUTTER_TERMUX_COMMANDS=" + json.dumps({name: ["arch", "mode", "jobs"]
                                                            for name in b.BACKEND_COMMANDS}) + "\n"
        return ""


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "builds/9.1.0"
        self.workspace.mkdir(parents=True)
        self.config = {"release": RELEASE, "backend_repo": b.DEFAULT_REPO,
                       "backend_ref": "main", "resume": False, "jobs": 1,
                       "reserve_gib": 0.000001}
        self.runner = FakeRunner(self.config, self.root)
        self.ndk = self.root / "ndk"
        self.ndk.mkdir()
        (self.ndk / "source.properties").write_text("Pkg.Revision = 29.0.1\n")
        self.patches = [patch.object(b, "provision_ndk", return_value=self.ndk),
                        patch.object(b.shutil, "which", return_value="/usr/bin/ninja"),
                        patch.object(b, "collect_artifacts", return_value=[{"file": "test.deb"}])]
        for p in self.patches:
            p.start()
        # Le collecteur réel crée le dossier de sortie avant d'écrire les preuves.
        self.artifacts = self.workspace / "artifacts/9.1.0"
        self.artifacts.mkdir(parents=True)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_pipeline_builds_all_modes_and_reuses_finished_steps(self):
        b.execute_build(self.config, self.runner, self.workspace)
        calls = self.runner.commands
        modes = [next(x for x in c if x.startswith("--mode="))
                 for c in calls if len(c) > 2 and c[1:3] == ["build.py", "build"]]
        self.assertEqual(modes, ["--mode=release", "--mode=debug", "--mode=profile"])
        android = [c for c in calls if len(c) > 2 and c[1:3] == ["build.py", "build_android"]]
        self.assertEqual(len(android), 2)
        self.assertTrue(all("--jobs=1" in c for c in android))
        cfg = tomllib.loads((self.workspace / "backend/build.toml").read_text())
        self.assertEqual(cfg["flutter"]["devtools_version"], METADATA["devToolsVersion"])
        self.assertEqual(cfg["flutter"]["framework_revision"], HASH)
        before = len(calls)
        self.config["resume"] = True
        b.execute_build(self.config, self.runner, self.workspace)
        after = calls[before:]
        self.assertFalse(any(len(c) > 2 and c[1] == "build.py" for c in after))
        evidence = json.loads((self.artifacts / "build-evidence.json").read_text())
        self.assertEqual(evidence["termux_device_test"], "not_run")
        self.assertEqual(evidence["backend_commit"], BACKEND_HASH)

    def test_missing_profile_stops_before_packaging_and_rebuild_from_8_recovers(self):
        self.runner.omit_profile = True
        with self.assertRaisesRegex(b.BuildError, "linux_profile_arm64"):
            b.execute_build(self.config, self.runner, self.workspace)
        self.assertFalse(any(c[1:3] == ["build.py", "debuild"] for c in self.runner.commands))
        self.runner.omit_profile = False
        self.config.update(resume=True, rebuild_from=8)
        b.execute_build(self.config, self.runner, self.workspace)
        self.assertEqual(json.loads((self.workspace / "state.json").read_text())["current"], "complete")

    def test_resume_rejects_a_different_official_version_hash(self):
        b.execute_build(self.config, self.runner, self.workspace)
        self.config.update(resume=True, release=dict(RELEASE, hash="0" * 40))
        with self.assertRaisesRegex(b.BuildError, "Configuration différente"):
            b.execute_build(self.config, self.runner, self.workspace)


if __name__ == "__main__":
    unittest.main()
