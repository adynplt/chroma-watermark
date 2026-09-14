// Spread-spectrum frame watermark for the Win32/DirectX11 ImGui example.
//
// Embeds a 32-bit payload (a match ID) into the rendered frame as a low
// spatial frequency blue-yellow chroma perturbation, weighted by a
// perceptual model so it sits where the picture hides it, keyed by a shared
// secret. The mark is
// designed to survive lossy re-encoding and, with enough strength and temporal
// accumulation, an off-screen recording made with a phone camera.
//
// Usage: initialise once after the D3D device exists, call Apply() each frame
// between the ImGui render pass and Present, and release on shutdown.
#pragma once

#include <d3d11.h>
#include <stdint.h>

// Layout of the embedded signal. The decoder must be built with identical
// values or correlation will not recover the payload.
namespace WatermarkLayout
{
    // Payload carried by the mark, in bits: the match ID.
    constexpr int PayloadBits = 32;

    // A CRC-8 of the payload is embedded alongside it, so the decoder can tell
    // a correct recovery from a plausible-looking wrong one. Without it a
    // single flipped bit yields a valid-looking match ID with no warning.
    constexpr int CrcBits = 8;
    constexpr int CodedBits = PayloadBits + CrcBits;

    // Each coded bit is repeated across this many cells, so the decoder only
    // needs the average of the copies to survive rather than any single one.
    // Fewer copies than the original 32 to make room for the pilots; the
    // decoder improvements made since (trimmed voting, occlusion rejection)
    // more than cover the difference.
    constexpr int RepeatPerBit = 22;
    constexpr int DataCells = CodedBits * RepeatPerBit;

    // Pilot cells carry a fixed, key-derived sign that does not depend on the
    // payload. They serve two purposes: detecting whether a mark is present at
    // all (an unmarked image otherwise decodes to a confident-looking random
    // ID), and anchoring the geometric search that lets the decoder find the
    // grid in a rotated, scaled or perspective-distorted capture.
    constexpr int PilotCells = 144;

    constexpr int TotalCells = DataCells + PilotCells;

    // The frame is divided into a grid of cells; each cell carries one copy of
    // one bit, or one pilot, as a smooth chroma bump. Cells are deliberately
    // large: optical blur and the downsampling of a camera capture destroy
    // fine detail, while a coarse pattern survives.
    constexpr int GridCols = 32;
    constexpr int GridRows = 32;

    static_assert(GridCols * GridRows == TotalCells,
                  "data cells plus pilots must fill the grid exactly");
}

class FrameWatermark
{
public:
    bool Initialise(ID3D11Device* device, ID3D11DeviceContext* context);
    void Release();

    // Recreates the intermediate render target. Call on startup and whenever
    // the swap chain is resized.
    bool ResizeBuffers(UINT width, UINT height);

    // Render target that the frame should be drawn into when the watermark is
    // enabled, in place of the swap chain's own view.
    ID3D11RenderTargetView* SceneTarget() const { return m_sceneRTV; }

    // Draws the scene texture to the given target, adding the keyed chroma
    // pattern for the current payload.
    void Apply(ID3D11RenderTargetView* destination);

    void SetPayload(uint32_t payload);
    uint32_t Payload() const { return m_payload; }

    // Peak chroma offset at a cell centre, as a fraction of channel range.
    // The value that survives a camera capture has to be found by
    // measurement; see WATERMARK.md.
    void SetStrength(float strength) { m_strength = strength; }
    float Strength() const { return m_strength; }

    void SetEnabled(bool enabled) { m_enabled = enabled; }
    bool Enabled() const { return m_enabled; }

    // When set, the sign of the whole pattern flips every
    // kPolarityHalfPeriodMs of wall-clock time. The eye averages a low
    // contrast chroma flicker at that rate to nothing, so the mark can be
    // stronger for the same visibility on a live display. The decoder
    // resolves each captured frame's polarity from the pilots. The cost is
    // that a camera exposure spanning both polarities cancels the mark: at
    // a 25 ms half period, exposures of 1/60 s and shorter (the usual case
    // when filming a bright screen) catch one polarity, and 1/30 s and
    // longer lose most of it. Off by default for that reason.
    void SetAlternatePolarity(bool alternate) { m_alternatePolarity = alternate; }
    bool AlternatePolarity() const { return m_alternatePolarity; }
    static constexpr double kPolarityHalfPeriodMs = 25.0;

private:
    // Uploads the per-cell signs derived from the current payload and key.
    void UpdatePatternBuffer();
    float CurrentPolarity() const;

    ID3D11Device* m_device = nullptr;
    ID3D11DeviceContext* m_context = nullptr;

    ID3D11Texture2D* m_sceneTexture = nullptr;
    ID3D11RenderTargetView* m_sceneRTV = nullptr;
    ID3D11ShaderResourceView* m_sceneSRV = nullptr;

    ID3D11VertexShader* m_vertexShader = nullptr;
    ID3D11PixelShader* m_pixelShader = nullptr;
    ID3D11Buffer* m_constantBuffer = nullptr;
    ID3D11SamplerState* m_sampler = nullptr;
    ID3D11BlendState* m_blendState = nullptr;
    ID3D11RasterizerState* m_rasteriser = nullptr;
    ID3D11DepthStencilState* m_depthStencilState = nullptr;

    UINT m_width = 0;
    UINT m_height = 0;

    uint32_t m_payload = 0;
    float m_strength = 0.08f;
    bool m_enabled = true;
    bool m_alternatePolarity = false;
    bool m_patternDirty = true;
};
