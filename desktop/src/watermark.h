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
    //
    // `format` is the format of the render target view the application draws
    // with and later passes to Apply(): the swap chain's view format, which on
    // a flip-model chain may be the sRGB twin of the buffer format. The
    // intermediate is created so that drawing into it behaves exactly as
    // drawing into that view would. Supported: R8G8B8A8 and B8G8R8A8, each in
    // UNORM and UNORM_SRGB, and R10G10B10A2_UNORM. Anything else returns
    // false; in particular float HDR targets are refused, because the mark is
    // defined in display-referred 0..1 units.
    bool ResizeBuffers(UINT width, UINT height, DXGI_FORMAT format = DXGI_FORMAT_R8G8B8A8_UNORM);

    // Render target that the frame should be drawn into when the watermark is
    // enabled, in place of the swap chain's own view.
    ID3D11RenderTargetView* SceneTarget() const { return m_sceneRTV; }

    // The texture behind SceneTarget(), for tests that want to read back the
    // unmarked frame.
    ID3D11Texture2D* SceneTexture() const { return m_sceneTexture; }

    // Draws the scene texture to the given target, adding the keyed chroma
    // pattern for the current payload. Must be the last draw before Present.
    //
    // Every pipeline slot the pass binds is saved on entry and put back on
    // exit (see SetRestoresState), with two exceptions that Direct3D itself
    // imposes: pixel-shader UAVs are unbound by the render target change and
    // are not put back, and any shader resource view over the destination's
    // own resource is unbound by the hazard rules when the destination is set
    // as a render target.
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

    // Whether Apply() saves the pipeline state it overwrites and restores it
    // afterwards. On by default. Turn it off only if the caller already
    // brackets Apply() with its own state push and pop; the pass then still
    // unbinds its own shader resources on exit, as it always has.
    void SetRestoresState(bool restore) { m_restoreState = restore; }
    bool RestoresState() const { return m_restoreState; }

    // GPU time of the most recent Apply() whose timestamp queries have
    // resolved, in milliseconds, or a negative value until the first one has.
    // Queries resolve a few frames after they are issued and only once the
    // command stream has been submitted, so a caller that never presents or
    // flushes never gets a sample.
    float LastPassMilliseconds() const { return m_lastPassMs; }

private:
    // Uploads the per-cell signs derived from the current payload and key.
    void UpdatePatternBuffer(bool linearOutput);
    float CurrentPolarity() const;

    // Timestamp query bookkeeping; see Apply().
    bool CreateTimingQueries();
    void ReleaseTimingQueries();
    void ReadTimingQueries();

    // Everything Apply() binds, captured before and put back after. Kept as a
    // member because the class-instance arrays are too large for the stack
    // to be a comfortable place for them every frame.
    struct SavedPipelineState
    {
        static constexpr UINT kSavedShaderResources = 2;
        static constexpr UINT kMaxClassInstances = 256;

        ID3D11RenderTargetView* renderTargets[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT];
        ID3D11DepthStencilView* depthStencil;
        UINT viewportCount;
        D3D11_VIEWPORT viewports[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
        ID3D11RasterizerState* rasteriser;
        ID3D11DepthStencilState* depthStencilState;
        UINT stencilRef;
        ID3D11BlendState* blendState;
        float blendFactor[4];
        UINT sampleMask;
        ID3D11InputLayout* inputLayout;
        D3D11_PRIMITIVE_TOPOLOGY topology;
        ID3D11VertexShader* vertexShader;
        ID3D11PixelShader* pixelShader;
        ID3D11GeometryShader* geometryShader;
        ID3D11HullShader* hullShader;
        ID3D11DomainShader* domainShader;
        ID3D11ClassInstance* vertexInstances[kMaxClassInstances];
        ID3D11ClassInstance* pixelInstances[kMaxClassInstances];
        ID3D11ClassInstance* geometryInstances[kMaxClassInstances];
        ID3D11ClassInstance* hullInstances[kMaxClassInstances];
        ID3D11ClassInstance* domainInstances[kMaxClassInstances];
        UINT vertexInstanceCount;
        UINT pixelInstanceCount;
        UINT geometryInstanceCount;
        UINT hullInstanceCount;
        UINT domainInstanceCount;
        ID3D11ShaderResourceView* shaderResources[kSavedShaderResources];
        ID3D11SamplerState* sampler;
        ID3D11Buffer* constantBuffer;

        void Capture(ID3D11DeviceContext* context);
        // Puts everything back and releases the references Capture took.
        // Views that belong to the watermark itself are left unbound, since
        // they become render targets again on the next frame.
        void Restore(ID3D11DeviceContext* context, ID3D11ShaderResourceView* const* ownedViews, UINT ownedCount);
    };

    ID3D11Device* m_device = nullptr;
    ID3D11DeviceContext* m_context = nullptr;

    ID3D11Texture2D* m_sceneTexture = nullptr;
    ID3D11RenderTargetView* m_sceneRTV = nullptr;
    ID3D11ShaderResourceView* m_sceneSRV = nullptr;
    DXGI_FORMAT m_format = DXGI_FORMAT_UNKNOWN;

    ID3D11VertexShader* m_vertexShader = nullptr;
    ID3D11PixelShader* m_pixelShader = nullptr;
    ID3D11Buffer* m_constantBuffer = nullptr;
    ID3D11SamplerState* m_sampler = nullptr;
    ID3D11BlendState* m_blendState = nullptr;
    ID3D11RasterizerState* m_rasteriser = nullptr;
    ID3D11DepthStencilState* m_depthStencilState = nullptr;

    // One set of queries per in-flight frame. Deeper than the default frame
    // latency of three, so a set is never reused before it has resolved.
    static constexpr int kTimingSets = 5;
    struct TimingSet
    {
        ID3D11Query* disjoint = nullptr;
        ID3D11Query* begin = nullptr;
        ID3D11Query* end = nullptr;
        bool pending = false;
    };
    TimingSet m_timing[kTimingSets];
    int m_timingNext = 0;
    float m_lastPassMs = -1.0f;

    SavedPipelineState m_saved = {};

    UINT m_width = 0;
    UINT m_height = 0;

    uint32_t m_payload = 0;
    float m_strength = 0.08f;
    bool m_enabled = true;
    bool m_alternatePolarity = false;
    bool m_restoreState = true;
    bool m_patternDirty = true;
    bool m_warnedFormatMismatch = false;
};
