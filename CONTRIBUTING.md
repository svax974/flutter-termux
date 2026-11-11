<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 svax974 -->

# Contributing

Bug reports and pull requests are welcome. Use Python 3.11+ and keep the launcher free of mandatory third-party Python dependencies.

Run the lightweight checks before submitting a change:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile flutter_termux.py tests/test_builder.py
python3 flutter_termux.py --help
```

For a build failure, report the launcher command, selected Flutter version, backend commit, operating system and architecture, Python version, target mode and the relevant error from `build.log`. Remove tokens, SSH connection details and other private information. Do not attach a `.env`, private key, full SDK or generated package to a source contribution.

Keep orchestration tests independent of network access and package installation. A successful mock pipeline test is not evidence that a new Flutter version compiles: report actual engine and device validation separately.

If a failure is in upstream engine patches or package resources, identify the backend revision and contribute the porting change upstream or in a compatible backend fork. Do not remove required artifacts or substitute Linux AOT tools for Android tools to bypass a validation failure.

Contributions to this repository are provided under its [MIT License](LICENSE). Third-party code keeps its original licence and notices; do not copy it into this repository under an incompatible licence declaration.
