#include "background.h"

#include <wincodec.h>
#include <vector>

#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")

namespace
{
    template <typename T>
    void SafeRelease(T*& object)
    {
        if (object)
        {
            object->Release();
            object = nullptr;
        }
    }

    // Decodes the file into tightly packed 32-bit RGBA.
    bool DecodeImageRGBA(const wchar_t* path, std::vector<unsigned char>& pixels, UINT& width, UINT& height)
    {
        IWICImagingFactory* factory = nullptr;
        IWICBitmapDecoder* decoder = nullptr;
        IWICBitmapFrameDecode* frame = nullptr;
        IWICFormatConverter* converter = nullptr;
        bool ok = false;

        if (SUCCEEDED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory)))
            && SUCCEEDED(factory->CreateDecoderFromFilename(path, nullptr, GENERIC_READ, WICDecodeMetadataCacheOnDemand, &decoder))
            && SUCCEEDED(decoder->GetFrame(0, &frame))
            && SUCCEEDED(factory->CreateFormatConverter(&converter))
            && SUCCEEDED(converter->Initialize(frame, GUID_WICPixelFormat32bppRGBA, WICBitmapDitherTypeNone,
                                               nullptr, 0.0, WICBitmapPaletteTypeCustom))
            && SUCCEEDED(converter->GetSize(&width, &height))
            && width > 0 && height > 0)
        {
            const UINT stride = width * 4;
            pixels.resize((size_t)stride * height);
            ok = SUCCEEDED(converter->CopyPixels(nullptr, stride, (UINT)pixels.size(), pixels.data()));
        }

        SafeRelease(converter);
        SafeRelease(frame);
        SafeRelease(decoder);
        SafeRelease(factory);
        return ok;
    }
}

bool LoadBackgroundTexture(ID3D11Device* device, const char* path,
                           ID3D11ShaderResourceView** viewOut, int* widthOut, int* heightOut)
{
    if (!device || !path || !viewOut)
        return false;

    const int wideLength = MultiByteToWideChar(CP_UTF8, 0, path, -1, nullptr, 0);
    if (wideLength <= 0)
        return false;
    std::vector<wchar_t> widePath((size_t)wideLength);
    MultiByteToWideChar(CP_UTF8, 0, path, -1, widePath.data(), wideLength);

    // COM may already be initialised by the host; S_FALSE and a changed
    // apartment mode both mean it is usable, and only a fresh initialisation
    // is paired with an uninitialise below.
    const HRESULT coInit = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    const bool ownsCom = SUCCEEDED(coInit);

    std::vector<unsigned char> pixels;
    UINT width = 0;
    UINT height = 0;
    const bool decoded = DecodeImageRGBA(widePath.data(), pixels, width, height);

    if (ownsCom)
        CoUninitialize();
    if (!decoded)
        return false;

    D3D11_TEXTURE2D_DESC textureDesc = {};
    textureDesc.Width = width;
    textureDesc.Height = height;
    textureDesc.MipLevels = 1;
    textureDesc.ArraySize = 1;
    textureDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    textureDesc.SampleDesc.Count = 1;
    textureDesc.Usage = D3D11_USAGE_IMMUTABLE;
    textureDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    D3D11_SUBRESOURCE_DATA initial = {};
    initial.pSysMem = pixels.data();
    initial.SysMemPitch = width * 4;

    ID3D11Texture2D* texture = nullptr;
    if (FAILED(device->CreateTexture2D(&textureDesc, &initial, &texture)))
        return false;

    ID3D11ShaderResourceView* view = nullptr;
    const HRESULT hr = device->CreateShaderResourceView(texture, nullptr, &view);
    texture->Release();
    if (FAILED(hr))
        return false;

    *viewOut = view;
    if (widthOut)
        *widthOut = (int)width;
    if (heightOut)
        *heightOut = (int)height;
    return true;
}
