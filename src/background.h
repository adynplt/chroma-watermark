// Loads an image file into a D3D11 texture, for use as the window's backdrop.
//
// The example otherwise renders ImGui windows over a flat clear colour, which
// is the easiest possible content for the watermark: uniform panels read
// perfectly and hide nothing. A photograph behind the UI gives the decoder the
// kind of texture, edges and gradients a real application has.
#pragma once

#include <d3d11.h>

// Decodes any format Windows Imaging Component understands (JPEG, PNG, BMP,
// ...) into an immutable RGBA8 texture and returns a shader resource view for
// it. Returns false, leaving the outputs untouched, if the file cannot be read.
bool LoadBackgroundTexture(ID3D11Device* device, const char* path,
                           ID3D11ShaderResourceView** viewOut, int* widthOut, int* heightOut);
