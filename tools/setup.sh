#!/usr/bin/env bash
# Set up the build environment for the Cardboard Face Mask pipeline (Linux and macOS).
# Safe to re-run: every step checks what is already there and skips it.
#
#   tools/setup.sh                 venv + Python packages + NiflyDLL (+ ML) + Blender (Linux) + checks
#   tools/setup.sh --no-ml         skip torch/transformers (only needed to run src/01_segment.py)
#   tools/setup.sh --no-blender    skip the Blender download (only needed for preview renders)
#   tools/setup.sh --rebuild-nifly rebuild NiflyDLL even if one is installed
#   PYTHON=/path/to/python3.11 tools/setup.sh   choose the interpreter for .venv
#
# The ML packages are optional: if they cannot be installed (e.g. PyTorch 2.14.1 has macOS wheels
# only for Apple Silicon on macOS 14+), setup continues with a warning; then copy work/outline.json
# and work/matte.png from a machine that ran the segment stage and build with --skip-ml.
# Windows: use WSL2 (Ubuntu) and run this script there, or follow "Rebuild on Windows" in README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BLENDER_VERSION="4.2.14"
BLENDER_URL="https://download.blender.org/release/Blender4.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz"
# from https://download.blender.org/release/Blender4.2/blender-4.2.14.sha256 (fetched 2026-10-03)
BLENDER_SHA256="2f6177be854683f0bf8818710fd64d8e6752cea16d058097b9223017d5b97aa9"
BLENDER_MAC_ARM_URL="https://download.blender.org/release/Blender4.2/blender-${BLENDER_VERSION}-macos-arm64.dmg"
BLENDER_MAC_X64_URL="https://download.blender.org/release/Blender4.2/blender-${BLENDER_VERSION}-macos-x64.dmg"
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"
NIFLY_DLL="third_party/PyNifly/io_scene_nifly/NiflyDLL.dll"

WITH_ML=1
WITH_BLENDER=1
REBUILD_NIFLY=0
for arg in "$@"; do
  case "$arg" in
    --no-ml) WITH_ML=0 ;;
    --no-blender) WITH_BLENDER=0 ;;
    --rebuild-nifly) REBUILD_NIFLY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"
ARCH="$(uname -m)"
MACOS_MAJOR=0
if [ "$OS" = Darwin ]; then MACOS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"; fi
case "$MACOS_MAJOR" in ''|*[!0-9]*) MACOS_MAJOR=0 ;; esac
WARNINGS=()

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; WARNINGS+=("$*"); }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

sha256_of() {
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

case "$OS" in
  Linux|Darwin) ;;
  *) die "unsupported OS '$OS'; on Windows use WSL2 or the manual steps in README.md" ;;
esac

# --------------------------------------------------------------------------- Python
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  # 3.11 matches the reference environment the requirements were pinned in
  for candidate in python3.11 python3.12 python3.13 python3.10 python3; do
    if have "$candidate"; then echo "$candidate"; return; fi
  done
  die "no python3 found; install Python 3.11 (macOS: brew install python@3.11)"
}

step "Python virtual environment (.venv)"
if [ -x .venv/bin/python ]; then
  echo ".venv exists: $(.venv/bin/python --version)"
else
  PY="$(pick_python)"
  "$PY" -c 'import sys; v = sys.version_info; sys.exit(0 if (3, 10) <= v[:2] <= (3, 13) else 1)' \
    || die "$PY is $("$PY" --version 2>&1); need Python 3.10-3.13 (3.11 recommended)"
  echo "creating .venv with $("$PY" --version)"
  "$PY" -m venv .venv
fi
VPY="$ROOT/.venv/bin/python"

step "Python packages (requirements.txt)"
if [ "$OS" = Darwin ]; then
  # opencv-python-headless 5.0.0.93 publishes macOS wheels for arm64 on macOS 13+ and x86_64 on 14+
  if { [ "$ARCH" = arm64 ] && [ "$MACOS_MAJOR" -lt 13 ]; } || { [ "$ARCH" != arm64 ] && [ "$MACOS_MAJOR" -lt 14 ]; }; then
    warn "macOS $MACOS_MAJOR on $ARCH: opencv-python-headless has no wheel here, pip will try to build it from source (slow, may fail)"
  fi
fi
"$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo "ok"

# --------------------------------------------------------------------------- NiflyDLL
step "NiflyDLL (PyNifly's NIF library, built for $OS)"
if [ -f "$NIFLY_DLL" ] && [ "$REBUILD_NIFLY" = 0 ]; then
  echo "$NIFLY_DLL exists (use --rebuild-nifly to rebuild)"
else
  for tool in git cmake; do have "$tool" || die "'$tool' is required to build NiflyDLL (Linux: apt install $tool; macOS: brew install $tool)"; done
  if ! have c++ && ! have g++ && ! have clang++; then
    die "a C++20 compiler is required (Linux: apt install g++; macOS: xcode-select --install)"
  fi
  "$VPY" tools/nifly/build_niflydll.py
fi
"$VPY" -c "import sys; sys.path.insert(0, 'src'); from cfm.nifenv import NifFile; print('pyn import ok')"

# --------------------------------------------------------------------------- ML (optional)
# Only src/01_segment.py needs these. A failure here is a warning, not fatal: everything else
# builds without them (build_all.py --skip-ml with an existing work/outline.json + work/matte.png).
ML_SKIP_HINT="build with src/build_all.py --skip-ml after copying work/outline.json and work/matte.png from a machine that ran the segment stage"
install_ml() {
  if [ "$OS" = Linux ]; then
    TORCH_PIN="$(grep -E '^torch==' requirements-ml.txt | head -1)"
    TV_PIN="$(grep -E '^torchvision==' requirements-ml.txt | head -1)"
    if ! "$VPY" -c "import torch, sys; sys.exit(0 if torch.__version__.split('+')[0] == '${TORCH_PIN#torch==}' else 1)" 2>/dev/null; then
      echo "installing CPU-only $TORCH_PIN / $TV_PIN from $TORCH_CPU_INDEX"
      "$VPY" -m pip install --quiet --disable-pip-version-check --index-url "$TORCH_CPU_INDEX" "$TORCH_PIN" "$TV_PIN" || return 1
    fi
  fi
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements-ml.txt || return 1
  echo "ok ($("$VPY" -c 'import torch; print("torch", torch.__version__)'))"
}

step "ML packages (requirements-ml.txt, segment stage only)"
if [ "$WITH_ML" = 0 ]; then
  echo "skipping ML packages (--no-ml): src/01_segment.py will not run; $ML_SKIP_HINT"
elif [ "$OS" = Darwin ] && { [ "$ARCH" != arm64 ] || [ "$MACOS_MAJOR" -lt 14 ]; }; then
  # PyPI has torch 2.14.1 macOS wheels only for macosx_14_0_arm64 (checked 2026-10-03)
  warn "skipping ML packages: PyTorch $(grep -E '^torch==' requirements-ml.txt | cut -d= -f3) needs Apple Silicon and macOS 14+ (this is macOS $MACOS_MAJOR on $ARCH); $ML_SKIP_HINT"
elif ! install_ml; then
  warn "installing requirements-ml.txt failed (see the pip output above); src/01_segment.py will not run; $ML_SKIP_HINT"
fi

# --------------------------------------------------------------------------- Blender
step "Blender ${BLENDER_VERSION} LTS (headless preview renders)"
if [ "$WITH_BLENDER" = 0 ]; then
  echo "skipped (--no-blender)"
elif [ "$OS" = Linux ]; then
  if [ "$ARCH" != x86_64 ]; then
    warn "Blender only publishes linux-x64 builds; on $ARCH install Blender ${BLENDER_VERSION} yourself and set BLENDER=/path/to/blender"
  elif [ -x third_party/blender/blender ] && third_party/blender/blender --version 2>/dev/null | head -1 | grep -q "Blender ${BLENDER_VERSION}"; then
    echo "third_party/blender: $(third_party/blender/blender --version 2>/dev/null | head -1)"
  else
    have curl || die "curl is required to download Blender"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "downloading $BLENDER_URL"
    curl -fL --retry 3 -o "$TMP/blender.tar.xz" "$BLENDER_URL"
    GOT="$(sha256_of "$TMP/blender.tar.xz")"
    [ "$GOT" = "$BLENDER_SHA256" ] || die "Blender download checksum mismatch ($GOT)"
    rm -rf third_party/blender.new
    mkdir -p third_party/blender.new
    tar -xJf "$TMP/blender.tar.xz" -C third_party/blender.new --strip-components=1
    rm -rf third_party/blender
    mv third_party/blender.new third_party/blender
    echo "installed: $(third_party/blender/blender --version 2>/dev/null | head -1)"
  fi
else
  if [ -n "${BLENDER:-}" ] && [ -x "${BLENDER}" ]; then
    echo "using \$BLENDER=$BLENDER: $("$BLENDER" --version 2>/dev/null | head -1)"
  else
    if [ "$ARCH" = arm64 ]; then MAC_URL="$BLENDER_MAC_ARM_URL"; else MAC_URL="$BLENDER_MAC_X64_URL"; fi
    cat <<EOF
On macOS, install Blender yourself (renders only; the mod builds without it):
  1. download $MAC_URL
     (checksums: https://download.blender.org/release/Blender4.2/blender-${BLENDER_VERSION}.sha256)
  2. open the .dmg and drag Blender into /Applications
  3. export BLENDER=/Applications/Blender.app/Contents/MacOS/Blender   (add it to ~/.zshrc)
EOF
  fi
fi

# --------------------------------------------------------------------------- .NET
step ".NET SDK (plugin generator + Spriggit)"
if have dotnet; then
  SDKS="$(dotnet --list-sdks 2>/dev/null || true)"
  echo "$SDKS"
  if echo "$SDKS" | grep -Eq '^(1[0-9]|[2-9][0-9])\.'; then
    dotnet tool restore >/dev/null && echo "local tools restored (Spriggit, see .config/dotnet-tools.json)"
  else
    warn ".NET SDK 10 or newer is needed for src/plugin (net10.0); install it from https://dotnet.microsoft.com/download"
  fi
else
  warn "dotnet not found: the plugin stage needs the .NET 10 SDK (https://dotnet.microsoft.com/download)"
fi

# --------------------------------------------------------------------------- summary
step "Done"
if [ "${#WARNINGS[@]}" -gt 0 ]; then
  printf 'Finished with %d warning(s):\n' "${#WARNINGS[@]}"
  for w in "${WARNINGS[@]}"; do printf '  - %s\n' "$w"; done
fi
echo "Next: .venv/bin/python src/build_all.py   (tests: .venv/bin/python -m pytest tests)"
