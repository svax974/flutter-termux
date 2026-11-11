<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 svax974 -->

# Third-party projects

The MIT licence in this repository covers the launcher, tests, configuration examples and documentation authored for this project. Upstream repositories and toolchains are fetched separately at build time; their code and licences are not replaced by this project's licence.

| Project | Role | Upstream licence information |
| --- | --- | --- |
| [Flutter for Termux](https://github.com/GeneralKaos666/flutter-for-termux) | Bionic patches, sysroot, engine build and packaging backend | [Repository licence](https://github.com/GeneralKaos666/flutter-for-termux/blob/main/LICENSE) |
| [Flutter](https://github.com/flutter/flutter) | Framework and engine sources | [Repository licence](https://github.com/flutter/flutter/blob/main/LICENSE) and component notices |
| [Dart SDK](https://github.com/dart-lang/sdk) | Compiler, runtime and SDK sources | [Repository licence](https://github.com/dart-lang/sdk/blob/main/LICENSE) and component notices |
| [Chromium depot_tools](https://chromium.googlesource.com/chromium/tools/depot_tools.git/) | Source synchronization and build tooling | [Repository licence](https://chromium.googlesource.com/chromium/tools/depot_tools.git/+/HEAD/LICENSE) |
| [Termux packages](https://github.com/termux/termux-packages) | Native libraries used to construct the target sysroot | Individual package licences and source repositories |
| [Android SDK / NDK](https://developer.android.com/studio) | Host tools and cross-compilation toolchain | [Android terms](https://developer.android.com/studio/terms), package licences and NDK notices |
| [Ubuntu container image](https://hub.docker.com/_/ubuntu) | Docker build environment | Individual distribution package licences |

Additional dependencies are downloaded by those projects and keep their respective notices. A generated `.deb` combines third-party components; this repository's MIT licence is not a licence declaration for the complete generated package. Preserve and review the notices shipped by the selected sources before redistributing generated artifacts.

The launcher records selected source revisions in its build evidence. Refer to the actual downloaded revision for its applicable licence text.
