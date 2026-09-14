#include "watermark.h"

#include <d3dcompiler.h>
#include <math.h>
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
        // in texels. w: unused.
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
    float2 r = texel.xy * texel.z;
    float eroded = 1e9;
    [unroll] for (int dy = -1; dy <= 1; ++dy)
    {
        [unroll] for (int dx = -1; dx <= 1; ++dx)
            eroded = min(eroded, Activity(input.uv + float2(dx, dy) * 2.0 * r, r));
    }
    float textureMask = smoothstep(maskParams.z, maskParams.w, eroded);
    float weight = saturate(dot(scene, kLumaWeights) / maskParams.x)
                 * (maskParams.y + (1.0 - maskParams.y) * textureMask);

    float3 offset = kChromaAxis * (amplitude * strength * profile * weight * polarity);

    // Scale the offset down where it would push a channel past 0 or 1.
    float3 room = (offset > 0.0) ? (1.0 - scene) / max(offset, 1e-6)
                                 : ((offset < 0.0) ? scene / max(-offset, 1e-6) : 1e6);
    float scale = saturate(min(room.r, min(room.g, room.b)));

    return float4(saturate(scene + offset * scale), 1.0);
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
}

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

    return true;
}

bool FrameWatermark::ResizeBuffers(UINT width, UINT height)
{
    if (width == 0 || height == 0)
        return false;

    if (m_sceneSRV) { m_sceneSRV->Release(); m_sceneSRV = nullptr; }
    if (m_sceneRTV) { m_sceneRTV->Release(); m_sceneRTV = nullptr; }
    if (m_sceneTexture) { m_sceneTexture->Release(); m_sceneTexture = nullptr; }

    D3D11_TEXTURE2D_DESC textureDesc = {};
    textureDesc.Width = width;
    textureDesc.Height = height;
    textureDesc.MipLevels = 1;
    textureDesc.ArraySize = 1;
    textureDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    textureDesc.SampleDesc.Count = 1;
    textureDesc.Usage = D3D11_USAGE_DEFAULT;
    textureDesc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(m_device->CreateTexture2D(&textureDesc, nullptr, &m_sceneTexture)))
        return false;
    if (FAILED(m_device->CreateRenderTargetView(m_sceneTexture, nullptr, &m_sceneRTV)))
        return false;
    if (FAILED(m_device->CreateShaderResourceView(m_sceneTexture, nullptr, &m_sceneSRV)))
        return false;

    m_width = width;
    m_height = height;
    return true;
}

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

void FrameWatermark::UpdatePatternBuffer()
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

    // The strength and polarity are uploaded every frame so the ImGui
    // controls take effect immediately; the pattern itself only changes when
    // the payload does.
    UpdatePatternBuffer();
    m_patternDirty = false;

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

    // Unbind the scene texture so it can be used as a render target again on
    // the next frame.
    ID3D11ShaderResourceView* const nullSRV[1] = { nullptr };
    m_context->PSSetShaderResources(0, 1, nullSRV);
}

void FrameWatermark::Release()
{
    if (m_depthStencilState) { m_depthStencilState->Release(); m_depthStencilState = nullptr; }
    if (m_rasteriser) { m_rasteriser->Release(); m_rasteriser = nullptr; }
    if (m_blendState) { m_blendState->Release(); m_blendState = nullptr; }
    if (m_sampler) { m_sampler->Release(); m_sampler = nullptr; }
    if (m_constantBuffer) { m_constantBuffer->Release(); m_constantBuffer = nullptr; }
    if (m_pixelShader) { m_pixelShader->Release(); m_pixelShader = nullptr; }
    if (m_vertexShader) { m_vertexShader->Release(); m_vertexShader = nullptr; }
    if (m_sceneSRV) { m_sceneSRV->Release(); m_sceneSRV = nullptr; }
    if (m_sceneRTV) { m_sceneRTV->Release(); m_sceneRTV = nullptr; }
    if (m_sceneTexture) { m_sceneTexture->Release(); m_sceneTexture = nullptr; }
}
