<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 svax974 -->

# Flutter Termux Builder

[![Tests](https://github.com/svax974/flutter-termux/actions/workflows/tests.yml/badge.svg)](https://github.com/svax974/flutter-termux/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Build a native **Flutter SDK for Termux ARM64** from source, on a local Linux machine, a Linux server over SSH, or a macOS machine using Docker.

The Python launcher reads Flutter's official release catalogue, offers the **five latest stable versions** with the newest selected by default, and lets you enter another published version. It drives the Termux porting backend, checks available disk space, installs missing prerequisites when authorized, streams build output, and supports resuming interrupted builds.

**Project status:** under development. Automated tests cover the launcher and a simulated build pipeline. Docker image creation and the initial backend checkout have been exercised; a complete engine build and installation on a physical Termux device have not yet been validated. A new Flutter release can require changes to upstream Termux patches. The launcher stops on incompatibility instead of silently selecting an older release.

[Documentation détaillée en français](docs/usage.fr.md)

## Features

- Latest versions come from the official Flutter catalogue, independently of a fork's binary releases.
- Interactive version menu, exact version selection and optional beta channel.
- Local Linux, SSH and Docker execution through the same worker.
- Disk check before source downloads, plus a reserve monitored during long commands.
- Optional prerequisite installation, isolated Python dependencies and Android SDK/NDK provisioning.
- Terminal logs, persistent checkpoints and incremental rebuilds.
- Separate Linux and Android AOT tool builds, with package content and ARM64 checks.
- `.deb` packages, SHA256 checksums and a JSON build evidence file.

## Requirements

The launcher needs **Python 3.11+** and uses only the standard library. Run it from bash, zsh or another shell; do not source the Python file.

| Build target | Requirements |
| --- | --- |
| Local Linux | Linux x86-64, Ubuntu 24.04 recommended; Debian 12 is a candidate but has not been validated with a complete build |
| SSH server | Same Linux environment, Python 3.11+, OpenSSH client on the launcher machine, key or agent authentication |
| macOS | Docker Desktop or Colima with a working local Docker context; builds run inside Linux x86-64 |
| Linux ARM64 | Docker with `linux/amd64` emulation, or an x86-64 server over SSH |

The default starting threshold is **150 GiB free** on the build filesystem, with a **10 GiB reserve**. These are configurable conservative limits, not a measured maximum build size. Docker's Linux disk must have enough free space independently of the host filesystem.

Ninja defaults to **one job** to reduce memory pressure. Even one linker job may require additional RAM or swap. The tool reports available memory and swap but does not create swap or modify `/etc/fstab`.

## Quick start

```bash
git clone https://github.com/svax974/flutter-termux.git
cd flutter-termux
cp .env.example .env
# Edit .env for your build machine.
python3 flutter_termux.py check
python3 flutter_termux.py build
```

The menu offers five releases and **6. Saisir une autre version** for manual entry. Press Enter to select the latest stable release. Manual versions are checked against the selected channel's official catalogue. Terminal messages are currently in French.

`check` performs host and resource checks without installing packages. Its exit code is `0` on success, `2` when system dependencies are missing, and `1` for blocking errors. The exact Android NDK is resolved during the build after fetching the backend. For Docker, `check` requires an existing builder image.

### Local Linux

```dotenv
BUILD_TARGET=local
WORK_DIR=~/.cache/flutter-termux
JOBS=1
```

```bash
python3 flutter_termux.py build
```

Missing system packages are installed with `apt-get` after confirmation, or automatically when configured. A non-root user needs suitable non-interactive sudo access for those installations, or an administrator can install the listed packages before retrying.

### macOS and Docker

```dotenv
BUILD_TARGET=local
DOCKER_CONTEXT=desktop-linux
```

```bash
python3 flutter_termux.py build
```

On macOS, `local` automatically selects Docker. You can also use `--target docker`. For Colima, use its context name instead of `desktop-linux`; inspect available contexts with `docker context ls`. The launcher uses the chosen context explicitly and does not change your global Docker context. Remote Docker endpoints are rejected for this local build mode.

On first use, the launcher offers to build the included [Dockerfile](Dockerfile). Sources and intermediate files stay in the Linux volume `flutter-termux-build`, avoiding macOS filesystem case and I/O issues. Packages are exported to `./artifacts/<version>/`.

Apple Silicon uses `linux/amd64` emulation and may be substantially slower than native Linux x86-64. Ensure Docker has sufficient memory and disk allocation. Docker installation itself is not automated.

To prepare only the image and run checks:

```bash
docker --context desktop-linux build --platform linux/amd64 \
  -t flutter-termux-builder:ubuntu24.04 .
python3 flutter_termux.py check --target docker
```

### Remote Linux over SSH

```dotenv
BUILD_TARGET=ssh
SSH_HOST=build.example.net
SSH_USER=builder
SSH_PORT=22
SSH_IDENTITY_FILE=~/.ssh/id_ed25519
WORK_DIR=/home/builder/flutter-termux-build
```

```bash
ssh -p 22 builder@build.example.net
python3 flutter_termux.py check
python3 flutter_termux.py build
```

An alias from `~/.ssh/config` also works. Establish the first connection and verify the server's host key before using the launcher. SSH runs in batch mode and does not store passwords. `SSH_PORT` overrides the port in SSH configuration. The worker is transferred automatically; the complete `.env` file and SSH credentials stay on the launcher machine.

Keep the launcher connected during the build. SSH mode runs in the foreground; it is not a detached build service. Use `tmux` on a machine that remains connected if you need to close your terminal.

## Configuration and automation

See [.env.example](.env.example) for all settings. `.env` is parsed as data: it is never executed as shell code. Do not run `source .env`. Exported environment variables override the file; command-line options override their corresponding settings. Paths for SDK/NDK reuse refer to the **build target**, not the launcher machine.

| Setting | Default | Purpose |
| --- | --- | --- |
| `BUILD_TARGET` | `local` | `local`, `ssh` or `docker` |
| `WORK_DIR` | `~/.cache/flutter-termux` | Dedicated build workspace; Docker uses `/work` |
| `MIN_FREE_GIB` | `150` | Minimum free space at startup, including resumes |
| `DISK_RESERVE_GIB` | `10` | Reserve monitored while commands run |
| `JOBS` | `1` | Ninja parallelism |
| `BACKEND_REPO` / `BACKEND_REF` | Termux backend / `main` | Porting repository and revision to fetch |
| `ANDROID_SDK_ROOT` / `ANDROID_NDK` | automatic | Reuse Android tools installed on the target |
| `NDK_VERSION` | backend setting | Explicit official host NDK version |
| `AUTO_INSTALL` | `false` | Install prerequisites without additional questions |
| `ACCEPT_ANDROID_LICENSES` | `false` | Accept Android SDK/NDK licences during provisioning |
| `DOCKER_CONTEXT` | active local context | Docker context to use explicitly |
| `ARTIFACT_DIR` | `./artifacts` | Local Docker artifact export |

To keep the version menu while skipping prerequisite questions:

```dotenv
AUTO_INSTALL=true
ACCEPT_ANDROID_LICENSES=true
```

The second setting records your acceptance for Android provisioning by this tool. Existing validated NDK selections are reused from the persistent SDK directory. An explicit `NDK_VERSION` is not substituted; if a backend references an unavailable repackaged NDK, an official host NDK from the same generation is proposed as part of installation.

```bash
# List releases without contacting the build target.
python3 flutter_termux.py versions
python3 flutter_termux.py versions --channel beta

# Interactive version selection, automatic prerequisites.
python3 flutter_termux.py build --auto-install --accept-android-licenses

# Fully automatic latest stable selection.
python3 flutter_termux.py build --yes --accept-android-licenses

# Pin an exact published version.
python3 flutter_termux.py build --version 3.47.9

python3 flutter_termux.py --help
```

## Build pipeline and outputs

This project is an orchestrator for [GeneralKaos666/flutter-for-termux](https://github.com/GeneralKaos666/flutter-for-termux), not a replacement for its Bionic port or patches. It selects an official Flutter release, pins backend and `depot_tools` commits for a build, synchronizes Flutter/Dart/DevTools metadata, and invokes the backend's stages.

It builds Linux **Release, Debug and Profile** variants and separate Android **Release and Profile** AOT tools. Linux `gen_snapshot` must not be substituted for the Android AOT tools. Before declaring success, the launcher checks package metadata and required ARM64 runtime resources and writes checksums and build evidence.

For local Linux and SSH, files are under:

```text
<WORK_DIR>/builds/<version>/
├── backend/
├── depot_tools/
├── venv/
├── build.log
├── state.json
├── flutter-metadata.json
└── artifacts/<version>/
    ├── flutter_*.deb
    ├── flutter_*.deb.sha256
    └── build-evidence.json
```

Docker exports artifacts into `ARTIFACT_DIR/<version>/` on the host. Logs and checkpoints remain in its named volume.

## Resume and troubleshooting

Use the same version when resuming, rather than letting the latest release change:

```bash
python3 flutter_termux.py build --version 3.47.9 --resume

# Replay engine builds onward, preserving Ninja's incremental outputs.
python3 flutter_termux.py build --version 3.47.9 --resume --rebuild-from 8

# Replay output checks and packaging only.
python3 flutter_termux.py build --version 3.47.9 --resume --rebuild-from 10
```

A workspace lock prevents simultaneous builds in the same directory. Changed source configuration requires another workspace. Interruptions preserve completed stage checkpoints; verify an earlier worker has stopped before retrying after a lost SSH connection.

If a newer Flutter release breaks the Termux patches, fix or select a compatible backend fork using `BACKEND_REPO` and `BACKEND_REF` with a new workspace. The launcher does not automatically rebase arbitrary source patches. Replaying `sync` on an already patched checkout can itself cause conflicts.

The [French guide](docs/usage.fr.md) covers additional SDK/NDK, resource and recovery details. When reporting a problem, include the command, host architecture, Python version and the relevant portion of `build.log`; remove private paths and connection information.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile flutter_termux.py tests/test_builder.py
python3 flutter_termux.py --help
```

Tests exercise release selection, configuration, disk failures, installation prompts, NDK reuse, callable backend commands, process interruption, package resources and a simulated pipeline. They do not download Flutter, install system packages or build an engine. GitHub Actions runs these checks on Linux and macOS.

See [CONTRIBUTING.md](CONTRIBUTING.md) for changes and bug reports. No credentials, `.env` files, SDKs, build logs or compiled packages should be committed.

## Device validation

A structurally valid ARM64 package still needs testing on a physical Android device. Follow the backend's Termux installation instructions, verify the package checksum, and test `flutter doctor`, project creation, debug/hot reload and a release APK build. Test Termux:X11 if using Linux desktop output.

The evidence file reports `termux_device_test: not_run`; this launcher does not perform on-device validation. Do not use upstream `flutter upgrade` over a patched Termux installation: rebuild and reinstall the package instead.

## License and acknowledgements

This launcher, its tests and its documentation are licensed under the **MIT License**; see [LICENSE](LICENSE). Copyright © 2026 [svax974](https://github.com/svax974).

Downloaded backends, Flutter/Dart/Chromium sources, Android tools and generated distributions retain their own licences. The MIT licence here does not relicense those components. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream projects and licence boundaries.

Flutter is a trademark of Google. This is an independent community project and is not an official Flutter or Termux distribution.
