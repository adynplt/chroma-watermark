#include "watermark.h"

#include <d3dcompiler.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#pragma comment(lib, "d3dcompiler.lib")

namespace
{
    // Shared secret. The decoder derives the same cell assignment and sign
    // sequence from this value, so the two must agree.
    constexpr uint32_t kWatermarkKey = 0x5A17C0DEu;

    // Integer avalanche hash. Written out explicitly rather than taken from a
    // standard library so that the C++ embedder and the Python decoder produce
    // bit-identical sequences.
    uint32_t HashU32(uint32_t x)
    {
        x ^= x >> 16;
        x *= 0x7FEB352Du;
        x ^= x >> 15;
        x *= 0x846CA68Bu;
        x ^= x >> 16;
        return x;
    }

    // CRC-8, polynomial 0x07, no reflection, zero init. Written out in full
    // so the Python decoder can carry an identical copy.
    uint32_t Crc8(uint32_t value)
    {
        uint32_t crc = 0;
        for (int byte = 0; byte < 4; ++byte)
        {
            crc ^= (value >> (8 * byte)) & 0xFFu;
            for (int bit = 0; bit < 8; ++bit)
                crc = (crc & 0x80u) ? ((crc << 1) ^ 0x07u) & 0xFFu : (crc << 1) & 0xFFu;
        }
        return crc;
    }

    // Which coded bit the given cell carries, and with which sign. Cells past
    // the data region are pilots: bitIndex is -1 and the sign is fixed.
    //
    // Data bits are assigned by division rather than by hashing. Hashing gives
    // an uneven split -- some bits land far more copies than others, and a bit
    // that draws few copies decodes unreliably -- so each bit is given exactly
    // RepeatPerBit consecutive cells instead. Which part of the frame those
    // cells occupy is still key-dependent, via the position permutation.
    //
    // Signs alternate within each bit's run so that every bit carries as many
    // positive cells as negative. A bit whose copies all shared one sign would
    // be indistinguishable from an overall brightness shift, which is exactly
    // what a camera's auto-exposure introduces.
    void DescribeCell(int cellIndex, int& bitIndex, float& sign)
    {
        if (cellIndex >= WatermarkLayout::DataCells)
        {
            bitIndex = -1;
            const uint32_t h = HashU32(kWatermarkKey ^ HashU32((uint32_t)cellIndex + 0x50110000u));
            sign = (h & 1u) ? -1.0f : 1.0f;
            return;
        }
        bitIndex = cellIndex / WatermarkLayout::RepeatPerBit;
        const int copyIndex = cellIndex % WatermarkLayout::RepeatPerBit;
        // Exactly half of each bit's copies are negative. The hash is keyed on
        // the bit rather than the cell, so it chooses which half without
        // disturbing the balance.
        const uint32_t h = HashU32(kWatermarkKey ^ HashU32((uint32_t)bitIndex));
        const bool secondHalf = (copyIndex >= WatermarkLayout::RepeatPerBit / 2);
        const bool flip = (h & 1u) != 0;
        sign = (secondHalf != flip) ? -1.0f : 1.0f;
    }

    // Maps grid cells to screen positions in a key-dependent order, so the
    // copies of a single bit are scattered rather than adjacent. Local damage
    // to the frame then costs each bit a little accuracy instead of costing
    // one bit all of its copies.
    //
    // This has to be a permutation: hashing into the grid directly would let
    // two cells collide, and a collision silently destroys one of the copies
    // the decoder expects to find. A keyed Fisher-Yates shuffle guarantees
    // every cell lands somewhere distinct. The decoder builds the same table
    // from the same key.
    const int* CellPositionTable()
    {
        constexpr int kCellCount = WatermarkLayout::GridCols * WatermarkLayout::GridRows;
        static int table[kCellCount];
        static bool built = false;
        if (!built)
        {
            for (int i = 0; i < kCellCount; ++i)
                table[i] = i;
            // Walk downwards so the index sequence matches the decoder's loop.
            for (int i = kCellCount - 1; i > 0; --i)
            {
                const uint32_t j = HashU32(kWatermarkKey + 0x9E3779B9u + (uint32_t)i) % (uint32_t)(i + 1);
                const int swap = table[i];
                table[i] = table[j];
                table[j] = swap;
            }
            built = true;
        }
        return table;
    }

    int ScrambleCellPosition(int cellIndex)
    {
        return CellPositionTable()[cellIndex];
    }

    struct WatermarkConstants
    {
        float strength;
        float gridCols;
        float gridRows;
        // +1 or -1; the whole pattern is multiplied by it. See
        // SetAlternatePolarity.
        float polarity;
        // x, y: size of one texel in UV units. z: radius of the activity taps
        // in texels. w: 1 when the destination view is sRGB and the shader
        // must hand back linear values for the hardware to re-encode, else 0.
        float texel[4];
        // Perceptual weighting: knee, floor, texture threshold low, high.
        // See kMask* below.
        float maskParams[4];
        // One float4 per cell; only .x is used, holding the signed amplitude.
        // Packed as float4 because HLSL constant buffer arrays are 16-byte
        // aligned regardless of element type.
        float cellAmplitude[WatermarkLayout::GridCols * WatermarkLayout::GridRows][4];
    };

    // Perceptual weighting of the mark. Must match MASK_* in
    // tools/decode_watermark.py, which recomputes the same weight from the
    // capture and uses it as each cell's expected amplitude.
    //
    // The amplitude at a pixel is strength * profile * weight, where
    //     weight = saturate(luma / kMaskKnee)
    //            * (kMaskFloor + (1 - kMaskFloor) * texture)
    // and texture is a smoothstep between kMaskTextureLow and
    // kMaskTextureHigh of the local luma activity after an erosion, so a
    // lone edge (text on a flat panel) does not count as texture.
    //
    // Dark pixels carry less: a fixed offset on a near-black panel reads as
    // a blue patch on black, and a camera records shadows with the most
    // noise anyway. Flat pixels carry kMaskFloor of the full amount, busy
    // ones all of it. The first two versions of this shader applied the
    // full amount everywhere (with a boost on edges) and read as a grid of
    // coloured tiles over any dark or smooth area.
    constexpr float kMaskKnee = 0.30f;
    constexpr float kMaskFloor = 0.30f;
    constexpr float kMaskTextureLow = 0.010f;
    constexpr float kMaskTextureHigh = 0.040f;
    // Activity tap radius as a fraction of a cell, so the measurement is the
    // same at any window size.
    constexpr float kMaskRadiusCells = 0.06f;

    // Fullscreen triangle generated from the vertex id; no vertex or index
    // buffer is bound.
    const char* kVertexShaderSource = R"(
struct VSOut
{
    float4 position : SV_POSITION;
    float2 uv       : TEXCOORD0;
};

VSOut main(uint id : SV_VertexID)
{
    VSOut output;
    output.uv = float2((id << 1) & 2, id & 2);
    output.position = float4(output.uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    return output;
}
)";

    // Adds a per-cell colour offset to the scene.
    //
    // The offset is a blue-yellow shift that leaves luma unchanged: +a on
    // blue, and enough taken from red and green to cancel it in Rec.601
    // luma. The eye is several times less sensitive to low-frequency chroma
    // than to luma, so the same visibility budget buys roughly three times
    // the amplitude.
    //
    // Each cell is a raised-cosine bump rather than a plateau, so there are
    // no edges between cells for the eye to pick out. The bump is scaled by
    // the perceptual weight described above and clamped so no channel
    // leaves its range.
    //
    // The scene is always read through a non-sRGB view, so every value here
    // is an encoded display code in 0..1, which is what the perceptual model
    // and the decoder assume. When the destination view is sRGB the hardware
    // would encode the result a second time, so the shader decodes it first;
    // encode(decode(x)) returns x exactly for every 8-bit code.
    const char* kPixelShaderSource = R"(
Texture2D    sceneTexture : register(t0);
SamplerState sceneSampler : register(s0);

cbuffer WatermarkConstants : register(b0)
{
    float  strength;
    float  gridCols;
    float  gridRows;
    float  polarity;
    float4 texel;
    float4 maskParams;
    float4 cellAmplitude[1024];
};

struct PSIn
{
    float4 position : SV_POSITION;
    float2 uv       : TEXCOORD0;
};

static const float3 kLumaWeights = float3(0.299, 0.587, 0.114);
// Blue-yellow direction with zero luma: +1 on blue, and red and green each
// reduced by 0.114 / (0.299 + 0.587).
static const float3 kChromaAxis = float3(-0.12867, -0.12867, 1.0);

float Luma(float2 uv)
{
    return dot(sceneTexture.Sample(sceneSampler, uv).rgb, kLumaWeights);
}

// Mean absolute luma difference to eight taps on a ring of radius r.
float Activity(float2 uv, float2 r)
{
    float centre = Luma(uv);
    float total = 0.0;
    total += abs(Luma(uv + float2( r.x,  0.0)) - centre);
    total += abs(Luma(uv + float2(-r.x,  0.0)) - centre);
    total += abs(Luma(uv + float2( 0.0,  r.y)) - centre);
    total += abs(Luma(uv + float2( 0.0, -r.y)) - centre);
    total += abs(Luma(uv + float2( r.x,  r.y)) - centre);
    total += abs(Luma(uv + float2( r.x, -r.y)) - centre);
    total += abs(Luma(uv + float2(-r.x,  r.y)) - centre);
    total += abs(Luma(uv + float2(-r.x, -r.y)) - centre);
    return total / 8.0;
}

// The exact sRGB transfer curve, not a gamma approximation: the hardware
// encoder is specified against this curve, and only the exact inverse
// round-trips every 8-bit code.
float3 SrgbToLinear(float3 c)
{
    float3 low = c / 12.92;
    float3 high = pow((c + 0.055) / 1.055, 2.4);
    return (c <= 0.04045) ? low : high;
}

float4 main(PSIn input) : SV_TARGET
{
    float3 scene = sceneTexture.Sample(sceneSampler, input.uv).rgb;

    int cellX = clamp((int)(input.uv.x * gridCols), 0, (int)gridCols - 1);
    int cellY = clamp((int)(input.uv.y * gridRows), 0, (int)gridRows - 1);
    float amplitude = cellAmplitude[cellY * (int)gridCols + cellX].x;

    // Raised-cosine bump over the cell: full strength at the centre, zero at
    // the edges, no step anywhere.
    float2 cellUV = frac(input.uv * float2(gridCols, gridRows));
    float2 s = sin(3.14159265 * cellUV);
    float profile = s.x * s.x * s.y * s.y;

    // Texture measure: local activity, eroded over a 3x3 of positions at
    // twice the tap radius, so a single edge with flat colour beside it
    // scores as flat.
    //
    // Two early-outs skip the activity reads where they cannot change the
    // result. A pixel too dark to carry anything has weight 0 whatever its
    // texture. And the erosion is a minimum, so once the centre position is
    // already at or below the lower threshold the smoothstep is exactly 0
    // no matter what the other eight positions read. Neither changes the
    // value at any pixel; on a frame of flat panels they remove most of the
    // 82 texture reads.
    float lumaWeight = saturate(dot(scene, kLumaWeights) / maskParams.x);
    float weight = 0.0;
    [branch] if (lumaWeight > 0.0)
    {
        float2 r = texel.xy * texel.z;
        float eroded = Activity(input.uv, r);
        [branch] if (eroded > maskParams.z)
        {
            [unroll] for (int dy = -1; dy <= 1; ++dy)
            {
                [unroll] for (int dx = -1; dx <= 1; ++dx)
                {
                    if (dx != 0 || dy != 0)
                        eroded = min(eroded, Activity(input.uv + float2(dx, dy) * 2.0 * r, r));
                }
            }
        }
        float textureMask = smoothstep(maskParams.z, maskParams.w, eroded);
        weight = lumaWeight * (maskParams.y + (1.0 - maskParams.y) * textureMask);
    }

    float3 offset = kChromaAxis * (amplitude * strength * profile * weight * polarity);

    // Scale the offset down where it would push a channel past 0 or 1.
    float3 room = (offset > 0.0) ? (1.0 - scene) / max(offset, 1e-6)
                                 : ((offset < 0.0) ? scene / max(-offset, 1e-6) : 1e6);
    float scale = saturate(min(room.r, min(room.g, room.b)));

    float3 marked = saturate(scene + offset * scale);
    if (texel.w > 0.5)
        marked = SrgbToLinear(marked);
    return float4(marked, 1.0);
}
)";

    bool CompileShader(const char* source, const char* target, ID3DBlob** blobOut)
    {
        ID3DBlob* errors = nullptr;
        const HRESULT hr = D3DCompile(source, strlen(source), nullptr, nullptr, nullptr,
                                      "main", target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
                                      blobOut, &errors);
        if (errors)
            errors->Release();
        return SUCCEEDED(hr);
    }

    template <typename T>
    void SafeRelease(T*& object)
    {
        if (object)
        {
            object->Release();
            object = nullptr;
        }
    }

    // ---- Format handling -------------------------------------------------

    bool IsSrgbFormat(DXGI_FORMAT format)
    {
        return format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB || format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
    }

    // The UNORM view format that reads the same bits without a transfer
    // curve; the format itself for anything that has no sRGB twin.
    DXGI_FORMAT NonSrgbTwin(DXGI_FORMAT format)
    {
        switch (format)
        {
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: return DXGI_FORMAT_R8G8B8A8_UNORM;
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB: return DXGI_FORMAT_B8G8R8A8_UNORM;
        default: return format;
        }
    }

    // The typeless resource format the views are cast from, or UNKNOWN when
    // the format has no sRGB twin and the texture can simply be typed.
    DXGI_FORMAT TypelessFamily(DXGI_FORMAT format)
    {
        switch (format)
        {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: return DXGI_FORMAT_R8G8B8A8_TYPELESS;
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB: return DXGI_FORMAT_B8G8R8A8_TYPELESS;
        default: return DXGI_FORMAT_UNKNOWN;
        }
    }

    bool IsSupportedFormat(DXGI_FORMAT format)
    {
        switch (format)
        {
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
        case DXGI_FORMAT_R10G10B10A2_UNORM:
            return true;
        default:
            return false;
        }
    }
}

// ---- Pipeline state save and restore ---------------------------------------

void FrameWatermark::SavedPipelineState::Capture(ID3D11DeviceContext* context)
{
    memset(this, 0, sizeof(*this));

    context->OMGetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, renderTargets, &depthStencil);
    viewportCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    context->RSGetViewports(&viewportCount, viewports);
    context->RSGetState(&rasteriser);
    context->OMGetDepthStencilState(&depthStencilState, &stencilRef);
    context->OMGetBlendState(&blendState, blendFactor, &sampleMask);
    context->IAGetInputLayout(&inputLayout);
    context->IAGetPrimitiveTopology(&topology);

    vertexInstanceCount = kMaxClassInstances;
    context->VSGetShader(&vertexShader, vertexInstances, &vertexInstanceCount);
    pixelInstanceCount = kMaxClassInstances;
    context->PSGetShader(&pixelShader, pixelInstances, &pixelInstanceCount);
    geometryInstanceCount = kMaxClassInstances;
    context->GSGetShader(&geometryShader, geometryInstances, &geometryInstanceCount);
    hullInstanceCount = kMaxClassInstances;
    context->HSGetShader(&hullShader, hullInstances, &hullInstanceCount);
    domainInstanceCount = kMaxClassInstances;
    context->DSGetShader(&domainShader, domainInstances, &domainInstanceCount);

    context->PSGetShaderResources(0, kSavedShaderResources, shaderResources);
    context->PSGetSamplers(0, 1, &sampler);
    context->PSGetConstantBuffers(0, 1, &constantBuffer);
}

void FrameWatermark::SavedPipelineState::Restore(ID3D11DeviceContext* context,
                                                 ID3D11ShaderResourceView* const* ownedViews, UINT ownedCount)
{
    // Targets first: the caller has already unbound the watermark's own
    // views, so putting the previous targets back cannot trip a hazard.
    context->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, renderTargets, depthStencil);
    context->RSSetViewports(viewportCount, viewports);
    context->RSSetState(rasteriser);
    context->OMSetDepthStencilState(depthStencilState, stencilRef);
    context->OMSetBlendState(blendState, blendFactor, sampleMask);
    context->IASetInputLayout(inputLayout);
    context->IASetPrimitiveTopology(topology);

    context->VSSetShader(vertexShader, vertexInstances, vertexInstanceCount);
    context->PSSetShader(pixelShader, pixelInstances, pixelInstanceCount);
    context->GSSetShader(geometryShader, geometryInstances, geometryInstanceCount);
    context->HSSetShader(hullShader, hullInstances, hullInstanceCount);
    context->DSSetShader(domainShader, domainInstances, domainInstanceCount);

    ID3D11ShaderResourceView* restoredViews[kSavedShaderResources];
    for (UINT slot = 0; slot < kSavedShaderResources; ++slot)
    {
        restoredViews[slot] = shaderResources[slot];
        for (UINT owned = 0; owned < ownedCount; ++owned)
            if (restoredViews[slot] && restoredViews[slot] == ownedViews[owned])
                restoredViews[slot] = nullptr;
    }
    context->PSSetShaderResources(0, kSavedShaderResources, restoredViews);
    context->PSSetSamplers(0, 1, &sampler);
    context->PSSetConstantBuffers(0, 1, &constantBuffer);

    for (UINT i = 0; i < D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT; ++i)
        SafeRelease(renderTargets[i]);
    SafeRelease(depthStencil);
    SafeRelease(rasteriser);
    SafeRelease(depthStencilState);
    SafeRelease(blendState);
    SafeRelease(inputLayout);
    SafeRelease(vertexShader);
    SafeRelease(pixelShader);
    SafeRelease(geometryShader);
    SafeRelease(hullShader);
    SafeRelease(domainShader);
    for (UINT i = 0; i < vertexInstanceCount; ++i) SafeRelease(vertexInstances[i]);
    for (UINT i = 0; i < pixelInstanceCount; ++i) SafeRelease(pixelInstances[i]);
    for (UINT i = 0; i < geometryInstanceCount; ++i) SafeRelease(geometryInstances[i]);
    for (UINT i = 0; i < hullInstanceCount; ++i) SafeRelease(hullInstances[i]);
    for (UINT i = 0; i < domainInstanceCount; ++i) SafeRelease(domainInstances[i]);
    for (UINT i = 0; i < kSavedShaderResources; ++i)
        SafeRelease(shaderResources[i]);
    SafeRelease(sampler);
    SafeRelease(constantBuffer);
}

// ---- Setup -------------------------------------------------------------------

bool FrameWatermark::Initialise(ID3D11Device* device, ID3D11DeviceContext* context)
{
    m_device = device;
    m_context = context;

    ID3DBlob* vertexBlob = nullptr;
    if (!CompileShader(kVertexShaderSource, "vs_4_0", &vertexBlob))
        return false;
    HRESULT hr = m_device->CreateVertexShader(vertexBlob->GetBufferPointer(),
                                              vertexBlob->GetBufferSize(), nullptr, &m_vertexShader);
    vertexBlob->Release();
    if (FAILED(hr))
        return false;

    ID3DBlob* pixelBlob = nullptr;
    if (!CompileShader(kPixelShaderSource, "ps_4_0", &pixelBlob))
        return false;
    hr = m_device->CreatePixelShader(pixelBlob->GetBufferPointer(),
                                     pixelBlob->GetBufferSize(), nullptr, &m_pixelShader);
    pixelBlob->Release();
    if (FAILED(hr))
        return false;

    D3D11_BUFFER_DESC bufferDesc = {};
    bufferDesc.ByteWidth = sizeof(WatermarkConstants);
    bufferDesc.Usage = D3D11_USAGE_DYNAMIC;
    bufferDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bufferDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(m_device->CreateBuffer(&bufferDesc, nullptr, &m_constantBuffer)))
        return false;

    D3D11_SAMPLER_DESC samplerDesc = {};
    samplerDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    samplerDesc.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    samplerDesc.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    samplerDesc.ComparisonFunc = D3D11_COMPARISON_ALWAYS;
    if (FAILED(m_device->CreateSamplerState(&samplerDesc, &m_sampler)))
        return false;

    D3D11_BLEND_DESC blendDesc = {};
    blendDesc.RenderTarget[0].BlendEnable = FALSE;
    blendDesc.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(m_device->CreateBlendState(&blendDesc, &m_blendState)))
        return false;

    D3D11_RASTERIZER_DESC rasteriserDesc = {};
    rasteriserDesc.FillMode = D3D11_FILL_SOLID;
    rasteriserDesc.CullMode = D3D11_CULL_NONE;
    rasteriserDesc.DepthClipEnable = TRUE;
    if (FAILED(m_device->CreateRasterizerState(&rasteriserDesc, &m_rasteriser)))
        return false;

    D3D11_DEPTH_STENCIL_DESC depthDesc = {};
    depthDesc.DepthEnable = FALSE;
    depthDesc.StencilEnable = FALSE;
    if (FAILED(m_device->CreateDepthStencilState(&depthDesc, &m_depthStencilState)))
        return false;

    // Timing is a convenience, not a requirement: a device that cannot
    // create the queries still watermarks, it just never reports a time.
    if (!CreateTimingQueries())
        ReleaseTimingQueries();

    return true;
}

bool FrameWatermark::ResizeBuffers(UINT width, UINT height, DXGI_FORMAT format)
{
    if (width == 0 || height == 0 || !IsSupportedFormat(format))
        return false;

    SafeRelease(m_sceneSRV);
    SafeRelease(m_sceneRTV);
    SafeRelease(m_sceneTexture);
    m_format = DXGI_FORMAT_UNKNOWN;

    const DXGI_FORMAT typeless = TypelessFamily(format);
    const DXGI_FORMAT viewFormat = NonSrgbTwin(format);

    UINT support = 0;
    if (FAILED(m_device->CheckFormatSupport(format, &support))
        || !(support & D3D11_FORMAT_SUPPORT_RENDER_TARGET))
        return false;
    if (FAILED(m_device->CheckFormatSupport(viewFormat, &support))
        || !(support & D3D11_FORMAT_SUPPORT_SHADER_SAMPLE))
        return false;

    D3D11_TEXTURE2D_DESC textureDesc = {};
    textureDesc.Width = width;
    textureDesc.Height = height;
    textureDesc.MipLevels = 1;
    textureDesc.ArraySize = 1;
    textureDesc.Format = typeless != DXGI_FORMAT_UNKNOWN ? typeless : format;
    textureDesc.SampleDesc.Count = 1;
    textureDesc.Usage = D3D11_USAGE_DEFAULT;
    textureDesc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(m_device->CreateTexture2D(&textureDesc, nullptr, &m_sceneTexture)))
        return false;

    if (typeless != DXGI_FORMAT_UNKNOWN)
    {
        // Views over a typeless resource must name their format. The render
        // target view carries the caller's format, sRGB or not, so drawing
        // into the intermediate behaves exactly as drawing into the swap
        // chain would; the shader resource view is always the plain UNORM
        // twin so the pass reads encoded display codes.
        D3D11_RENDER_TARGET_VIEW_DESC rtvDesc = {};
        rtvDesc.Format = format;
        rtvDesc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
        rtvDesc.Texture2D.MipSlice = 0;
        if (FAILED(m_device->CreateRenderTargetView(m_sceneTexture, &rtvDesc, &m_sceneRTV)))
            return false;

        D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = viewFormat;
        srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        srvDesc.Texture2D.MostDetailedMip = 0;
        srvDesc.Texture2D.MipLevels = 1;
        if (FAILED(m_device->CreateShaderResourceView(m_sceneTexture, &srvDesc, &m_sceneSRV)))
            return false;
    }
    else
    {
        if (FAILED(m_device->CreateRenderTargetView(m_sceneTexture, nullptr, &m_sceneRTV)))
            return false;
        if (FAILED(m_device->CreateShaderResourceView(m_sceneTexture, nullptr, &m_sceneSRV)))
            return false;
    }

    m_width = width;
    m_height = height;
    m_format = format;
    return true;
}

// ---- Timing ------------------------------------------------------------------

bool FrameWatermark::CreateTimingQueries()
{
    D3D11_QUERY_DESC disjointDesc = {};
    disjointDesc.Query = D3D11_QUERY_TIMESTAMP_DISJOINT;
    D3D11_QUERY_DESC timestampDesc = {};
    timestampDesc.Query = D3D11_QUERY_TIMESTAMP;

    for (TimingSet& set : m_timing)
    {
        if (FAILED(m_device->CreateQuery(&disjointDesc, &set.disjoint)))
            return false;
        if (FAILED(m_device->CreateQuery(&timestampDesc, &set.begin)))
            return false;
        if (FAILED(m_device->CreateQuery(&timestampDesc, &set.end)))
            return false;
        set.pending = false;
    }
    m_timingNext = 0;
    return true;
}

void FrameWatermark::ReleaseTimingQueries()
{
    for (TimingSet& set : m_timing)
    {
        SafeRelease(set.disjoint);
        SafeRelease(set.begin);
        SafeRelease(set.end);
        set.pending = false;
    }
    m_lastPassMs = -1.0f;
}

// Collects every resolved set, oldest first, and keeps the newest of them.
// Never waits: a set that has not resolved yet stops the walk, and the
// following sets cannot have resolved either.
void FrameWatermark::ReadTimingQueries()
{
    for (int step = 0; step < kTimingSets; ++step)
    {
        TimingSet& set = m_timing[(m_timingNext + step) % kTimingSets];
        if (!set.pending)
            continue;

        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint = {};
        const HRESULT hr = m_context->GetData(set.disjoint, &disjoint, sizeof(disjoint),
                                              D3D11_ASYNC_GETDATA_DONOTFLUSH);
        if (hr == S_FALSE)
            break;
        set.pending = false;
        if (FAILED(hr))
            continue;

        // Both timestamps were issued before the disjoint query ended, so
        // they have resolved with it.
        UINT64 begin = 0, end = 0;
        if (m_context->GetData(set.begin, &begin, sizeof(begin), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK
            || m_context->GetData(set.end, &end, sizeof(end), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK)
            continue;

        // A disjoint interval means the clock changed mid-measurement and the
        // numbers are meaningless; the previous value stands.
        if (!disjoint.Disjoint && disjoint.Frequency != 0 && end >= begin)
            m_lastPassMs = (float)((double)(end - begin) * 1000.0 / (double)disjoint.Frequency);
    }
}

// ---- Per-frame -----------------------------------------------------------------

void FrameWatermark::SetPayload(uint32_t payload)
{
    if (payload != m_payload)
    {
        m_payload = payload;
        m_patternDirty = true;
    }
}

float FrameWatermark::CurrentPolarity() const
{
    if (!m_alternatePolarity)
        return 1.0f;
    // Wall-clock driven so the rate does not depend on the frame rate: a
    // 30 fps renderer would otherwise flip every frame and a 240 fps one
    // eight times faster. Half period kPolarityHalfPeriodMs.
    LARGE_INTEGER counter, frequency;
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    const double milliseconds = 1000.0 * (double)counter.QuadPart / (double)frequency.QuadPart;
    const long long halfPeriods = (long long)(milliseconds / kPolarityHalfPeriodMs);
    return (halfPeriods & 1) ? -1.0f : 1.0f;
}

void FrameWatermark::UpdatePatternBuffer(bool linearOutput)
{
    WatermarkConstants constants = {};
    constants.strength = m_strength;
    constants.gridCols = (float)WatermarkLayout::GridCols;
    constants.gridRows = (float)WatermarkLayout::GridRows;
    constants.polarity = CurrentPolarity();
    constants.texel[0] = m_width ? 1.0f / (float)m_width : 0.0f;
    constants.texel[1] = m_height ? 1.0f / (float)m_height : 0.0f;
    const float cellPx = (float)(m_width < m_height ? m_width : m_height) / (float)WatermarkLayout::GridCols;
    float radius = floorf(kMaskRadiusCells * cellPx + 0.5f);
    constants.texel[2] = radius < 1.0f ? 1.0f : radius;
    constants.texel[3] = linearOutput ? 1.0f : 0.0f;
    constants.maskParams[0] = kMaskKnee;
    constants.maskParams[1] = kMaskFloor;
    constants.maskParams[2] = kMaskTextureLow;
    constants.maskParams[3] = kMaskTextureHigh;

    // Coded word: the payload in the low 32 bits, its CRC-8 above them.
    const uint64_t coded = (uint64_t)m_payload | ((uint64_t)Crc8(m_payload) << WatermarkLayout::PayloadBits);

    for (int cell = 0; cell < WatermarkLayout::TotalCells; ++cell)
    {
        int bitIndex = 0;
        float sign = 1.0f;
        DescribeCell(cell, bitIndex, sign);

        // A set bit raises luma in cells whose sign is positive and lowers it
        // where the sign is negative; a clear bit does the opposite. The
        // decoder recovers the bit from the sign of the correlation, which
        // makes the result independent of overall brightness. Pilots carry
        // their sign directly.
        float amplitude = sign;
        if (bitIndex >= 0)
        {
            const bool bitSet = (coded >> bitIndex) & 1u;
            amplitude = bitSet ? sign : -sign;
        }

        const int position = ScrambleCellPosition(cell);
        constants.cellAmplitude[position][0] = amplitude;
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (SUCCEEDED(m_context->Map(m_constantBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        memcpy(mapped.pData, &constants, sizeof(constants));
        m_context->Unmap(m_constantBuffer, 0);
    }
}

void FrameWatermark::Apply(ID3D11RenderTargetView* destination)
{
    if (!m_sceneSRV || !destination)
        return;

    // The destination decides whether the shader must hand back linear
    // values. The check is a CPU-side struct read; done every frame so a
    // caller that swaps views between frames is handled.
    D3D11_RENDER_TARGET_VIEW_DESC destinationDesc = {};
    destination->GetDesc(&destinationDesc);
    const bool linearOutput = IsSrgbFormat(destinationDesc.Format);
    if (!m_warnedFormatMismatch && NonSrgbTwin(destinationDesc.Format) != NonSrgbTwin(m_format))
    {
        // Not fatal: the pass still draws, but precision or channel order
        // may differ from what the intermediate was created for.
        char message[160];
        snprintf(message, sizeof(message),
                 "FrameWatermark: destination format %d differs from the ResizeBuffers format %d\n",
                 (int)destinationDesc.Format, (int)m_format);
        OutputDebugStringA(message);
        m_warnedFormatMismatch = true;
    }

    // The strength and polarity are uploaded every frame so the ImGui
    // controls take effect immediately; the pattern itself only changes when
    // the payload does.
    UpdatePatternBuffer(linearOutput);
    m_patternDirty = false;

    if (m_restoreState)
        m_saved.Capture(m_context);

    // Time the draw alone, on a set that is free; if the ring is full this
    // frame simply goes unmeasured.
    TimingSet& timing = m_timing[m_timingNext];
    const bool measure = timing.disjoint != nullptr && !timing.pending;
    if (measure)
    {
        m_context->Begin(timing.disjoint);
        m_context->End(timing.begin);
    }

    D3D11_VIEWPORT viewport = {};
    viewport.Width = (float)m_width;
    viewport.Height = (float)m_height;
    viewport.MaxDepth = 1.0f;

    m_context->OMSetRenderTargets(1, &destination, nullptr);
    m_context->RSSetViewports(1, &viewport);
    m_context->RSSetState(m_rasteriser);
    m_context->OMSetDepthStencilState(m_depthStencilState, 0);

    const float blendFactor[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    m_context->OMSetBlendState(m_blendState, blendFactor, 0xFFFFFFFF);

    m_context->IASetInputLayout(nullptr);
    m_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    m_context->VSSetShader(m_vertexShader, nullptr, 0);
    m_context->PSSetShader(m_pixelShader, nullptr, 0);
    m_context->PSSetShaderResources(0, 1, &m_sceneSRV);
    m_context->PSSetSamplers(0, 1, &m_sampler);
    m_context->PSSetConstantBuffers(0, 1, &m_constantBuffer);
    m_context->GSSetShader(nullptr, nullptr, 0);
    m_context->HSSetShader(nullptr, nullptr, 0);
    m_context->DSSetShader(nullptr, nullptr, 0);

    m_context->Draw(3, 0);

    if (measure)
    {
        m_context->End(timing.end);
        m_context->End(timing.disjoint);
        timing.pending = true;
        m_timingNext = (m_timingNext + 1) % kTimingSets;
    }

    // Unbind the scene texture so it can be used as a render target again on
    // the next frame. This happens before the previous targets go back, so
    // the restore cannot trip the render-target / shader-resource hazard.
    ID3D11ShaderResourceView* const nullViews[SavedPipelineState::kSavedShaderResources] = { nullptr, nullptr };
    m_context->PSSetShaderResources(0, SavedPipelineState::kSavedShaderResources, nullViews);

    if (m_restoreState)
    {
        ID3D11ShaderResourceView* const owned[1] = { m_sceneSRV };
        m_saved.Restore(m_context, owned, 1);
    }

    ReadTimingQueries();
}

void FrameWatermark::Release()
{
    ReleaseTimingQueries();
    SafeRelease(m_depthStencilState);
    SafeRelease(m_rasteriser);
    SafeRelease(m_blendState);
    SafeRelease(m_sampler);
    SafeRelease(m_constantBuffer);
    SafeRelease(m_pixelShader);
    SafeRelease(m_vertexShader);
    SafeRelease(m_sceneSRV);
    SafeRelease(m_sceneRTV);
    SafeRelease(m_sceneTexture);
    m_format = DXGI_FORMAT_UNKNOWN;
}
