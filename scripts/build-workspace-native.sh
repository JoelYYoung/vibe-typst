#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$(docker info --format '{{.Architecture}}')" in
  aarch64|arm64) arch=arm64;; x86_64|amd64) arch=amd64;; *) echo 'Unsupported Docker architecture' >&2; exit 1;;
esac
BUILD_DIR="${TCB_NATIVE_BUILD_DIR:-$ROOT/control/data/native-build-$arch}"
mkdir -p "$BUILD_DIR/cargo" "$BUILD_DIR/target" "$ROOT/resolver/target/native-linux-$arch/release"
# Native build output and cargo cache live on the host to avoid filling the VM disk.
docker run --rm --platform "linux/$arch" --cpus=2 --memory=4g \
  -v "$ROOT/resolver:/src:ro" -v "$BUILD_DIR/cargo:/cargo-cache" \
  -v "$BUILD_DIR/target:/target" -e CARGO_HOME=/cargo-cache -e CARGO_TARGET_DIR=/target \
  rust:1.95-bookworm bash -e -c '
    mkdir /build
    cp /src/Cargo.toml /src/Cargo.lock /build/
    cp -r /src/src /build/
    cd /build
    cargo build --release --locked --jobs 2
  '
cp "$BUILD_DIR/target/release/tcb-resolver" "$ROOT/resolver/target/native-linux-$arch/release/tcb-resolver"
docker build --platform "linux/$arch" -f "$ROOT/Containerfile.native" \
  -t "${TCB_NATIVE_IMAGE:-tcb-workspace:native-candidate}" "$ROOT"
