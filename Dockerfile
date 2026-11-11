# SPDX-License-Identifier: MIT
# Copyright (c) 2026 svax974
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates \
    build-essential ninja-build cmake pkg-config clang lld \
    dpkg-dev binutils xz-utils unzip zip zstd file patch \
    openjdk-21-jdk-headless libglib2.0-dev libgtk-3-dev \
    liblzma-dev libstdc++-12-dev libglu1-mesa-dev libfontconfig1-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
