#!/usr/bin/env bash
# Build the watermarked Dear ImGui Win32/DirectX11 example.
#
# The upstream vcxproj pins Windows SDK 8.1 and toolset v141. Neither is
# generally installed, so both are overridden on the command line; the clone
# stays untouched and Visual Studio never needs to "retarget" the solution.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${1:-Release}"
PROJECT_DIR="$ROOT/imgui/examples/example_win32_directx11"

# Override with MSBUILD=... if Visual Studio lives elsewhere.
MSBUILD="${MSBUILD:-/c/Program Files/Microsoft Visual Studio/18/Community/MSBuild/Current/Bin/MSBuild.exe}"
TOOLSET="${TOOLSET:-v145}"
SDK="${SDK:-10.0.26100.0}"

if [ ! -d "$PROJECT_DIR" ]; then
    echo "Dear ImGui is not set up yet. Run ./setup.sh first." >&2
    exit 1
fi

"$MSBUILD" "$PROJECT_DIR/example_win32_directx11.vcxproj" \
    -p:Configuration="$CONFIG" \
    -p:Platform=x64 \
    -p:WindowsTargetPlatformVersion="$SDK" \
    -p:PlatformToolset="$TOOLSET" \
    -v:minimal

echo "Built: $PROJECT_DIR/$CONFIG/example_win32_directx11.exe"
