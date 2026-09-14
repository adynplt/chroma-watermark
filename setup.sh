#!/usr/bin/env bash
# Fetch Dear ImGui and graft this project's example onto it.
#
# The repository carries only the files this project owns; Dear ImGui itself is
# cloned here at a known-good commit. Run this once after cloning, then build.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
IMGUI_DIR="$ROOT/imgui"
# The commit the example was developed and tested against. Any recent master
# works; this one is pinned so a build is reproducible.
IMGUI_COMMIT="e0a2f6dea98f76a793a6e62a2bdb7ee9b29f5add"
EXAMPLE_DIR="$IMGUI_DIR/examples/example_win32_directx11"

if [ ! -d "$IMGUI_DIR/.git" ]; then
    echo "Cloning Dear ImGui..."
    git clone https://github.com/ocornut/imgui.git "$IMGUI_DIR"
fi

echo "Checking out $IMGUI_COMMIT..."
git -C "$IMGUI_DIR" fetch --quiet origin
git -C "$IMGUI_DIR" checkout --quiet "$IMGUI_COMMIT"

echo "Installing watermark sources into the example..."
cp "$ROOT/src/watermark.cpp" "$ROOT/src/watermark.h" \
   "$ROOT/src/background.cpp" "$ROOT/src/background.h" \
   "$ROOT/src/main.cpp" "$EXAMPLE_DIR/"

# Add the two new translation units to the project file, unless already present.
PROJECT="$EXAMPLE_DIR/example_win32_directx11.vcxproj"
if ! grep -q "watermark.cpp" "$PROJECT"; then
    echo "Adding sources to the vcxproj..."
    python - "$PROJECT" <<'PYTHON'
import io
import sys

path = sys.argv[1]
text = io.open(path, encoding="utf-8-sig").read()
text = text.replace(
    '    <ClInclude Include="..\\..\\imgui.h" />',
    '    <ClInclude Include="..\\..\\imgui.h" />\n'
    '    <ClInclude Include="watermark.h" />\n'
    '    <ClInclude Include="background.h" />', 1)
text = text.replace(
    '    <ClCompile Include="main.cpp" />',
    '    <ClCompile Include="main.cpp" />\n'
    '    <ClCompile Include="watermark.cpp" />\n'
    '    <ClCompile Include="background.cpp" />', 1)
io.open(path, "w", encoding="utf-8-sig").write(text)
PYTHON
fi

echo
echo "Ready. Build with:  ./build.sh Release"
