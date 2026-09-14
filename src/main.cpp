// Dear ImGui: standalone example application for Windows API + DirectX 11

// Learn about Dear ImGui:
// - FAQ                  https://dearimgui.com/faq
// - Getting Started      https://dearimgui.com/getting-started
// - Documentation        https://dearimgui.com/docs (same as your local docs/ folder).
// - Introduction, links and more at the top of imgui.cpp

#include "imgui.h"
#include "imgui_impl_win32.h"
#include "imgui_impl_dx11.h"
#include <d3d11.h>
#include <tchar.h>

#include "watermark.h"
#include "background.h"

#include <stdio.h>
#include <stdlib.h>
#include <random>


// Data
static ID3D11Device*            g_pd3dDevice = nullptr;
static ID3D11DeviceContext*     g_pd3dDeviceContext = nullptr;
static IDXGISwapChain*          g_pSwapChain = nullptr;
static bool                     g_SwapChainOccluded = false;
static UINT                     g_ResizeWidth = 0, g_ResizeHeight = 0;
static ID3D11RenderTargetView*  g_mainRenderTargetView = nullptr;
static FrameWatermark           g_watermark;

// A random 32-bit match ID for this run, drawn from the system entropy source
// so it is not repeatable across launches. Used unless --capture supplies one.
static unsigned int RandomMatchId()
{
    std::random_device source;
    std::uniform_int_distribution<unsigned int> spread(0u, 0xFFFFFFFFu);
    return spread(source);
}

// Test hook: with "--capture <file> <match_id>" the example renders a few
// frames offscreen and writes the final backbuffer out, so the decoder can be
// checked against real shader output rather than a reimplementation of it.
static const char* g_capture_path = nullptr;
static unsigned int g_capture_id = 0;
static bool g_capture_random_id = false;
static int g_capture_frames = 0;
static float g_capture_strength = -1.0f;

// Backdrop image drawn beneath the ImGui windows, so the frames carry the
// texture and edges of a real application rather than a flat clear colour.
static ID3D11ShaderResourceView* g_backgroundView = nullptr;
static int g_backgroundWidth = 0;
static int g_backgroundHeight = 0;
static const char* g_background_arg = nullptr;
static char g_background_path[MAX_PATH] = "";

// Picks the backdrop: --background if given, else assets/background.jpg at
// the project root (four levels above the executable), else a stock Windows
// wallpaper, else none.
static bool ResolveBackgroundPath()
{
    const char* candidates[3] = { nullptr, nullptr, "C:\\Windows\\Web\\Wallpaper\\ThemeA\\img20.jpg" };
    candidates[0] = g_background_arg;

    char relative[MAX_PATH];
    char module_dir[MAX_PATH];
    if (::GetModuleFileNameA(nullptr, module_dir, MAX_PATH))
    {
        char* slash = strrchr(module_dir, '\\');
        if (slash)
            *slash = 0;
        snprintf(relative, sizeof(relative), "%s\\..\\..\\..\\..\\assets\\background.jpg", module_dir);
        candidates[1] = relative;
    }

    for (const char* candidate : candidates)
    {
        if (!candidate)
            continue;
        char full[MAX_PATH];
        if (!_fullpath(full, candidate, MAX_PATH))
            continue;
        if (::GetFileAttributesA(full) == INVALID_FILE_ATTRIBUTES)
            continue;
        strncpy(g_background_path, full, MAX_PATH - 1);
        return true;
    }
    return false;
}

// Draws the backdrop scaled to cover the viewport, keeping its aspect ratio
// and cropping the excess, under everything ImGui draws this frame.
static void DrawBackground(bool enabled)
{
    if (!enabled || !g_backgroundView || g_backgroundWidth <= 0 || g_backgroundHeight <= 0)
        return;
    const ImGuiViewport* viewport = ImGui::GetMainViewport();
    const ImVec2 origin = viewport->Pos;
    const ImVec2 size = viewport->Size;
    const float image_aspect = (float)g_backgroundWidth / (float)g_backgroundHeight;
    const float view_aspect = size.x / size.y;
    ImVec2 draw_size = size;
    if (view_aspect > image_aspect)
        draw_size.y = size.x / image_aspect;
    else
        draw_size.x = size.y * image_aspect;
    const ImVec2 p0(origin.x + (size.x - draw_size.x) * 0.5f, origin.y + (size.y - draw_size.y) * 0.5f);
    const ImVec2 p1(p0.x + draw_size.x, p0.y + draw_size.y);
    ImGui::GetBackgroundDrawList()->AddImage(ImTextureRef((ImTextureID)(intptr_t)g_backgroundView), p0, p1);
}

static void SaveBackbufferPPM(const char* path)
{
    ID3D11Texture2D* backbuffer = nullptr;
    if (FAILED(g_pSwapChain->GetBuffer(0, IID_PPV_ARGS(&backbuffer))))
        return;

    D3D11_TEXTURE2D_DESC desc = {};
    backbuffer->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;

    ID3D11Texture2D* staging = nullptr;
    if (SUCCEEDED(g_pd3dDevice->CreateTexture2D(&desc, nullptr, &staging)))
    {
        g_pd3dDeviceContext->CopyResource(staging, backbuffer);
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (SUCCEEDED(g_pd3dDeviceContext->Map(staging, 0, D3D11_MAP_READ, 0, &mapped)))
        {
            FILE* file = fopen(path, "wb");
            if (file)
            {
                fprintf(file, "P6\n%u %u\n255\n", desc.Width, desc.Height);
                for (UINT y = 0; y < desc.Height; ++y)
                {
                    const unsigned char* row = (const unsigned char*)mapped.pData + (size_t)y * mapped.RowPitch;
                    for (UINT x = 0; x < desc.Width; ++x)
                        fwrite(row + (size_t)x * 4, 1, 3, file);
                }
                fclose(file);
            }
            g_pd3dDeviceContext->Unmap(staging, 0);
        }
        staging->Release();
    }
    backbuffer->Release();
}

// Forward declarations of helper functions
bool CreateDeviceD3D(HWND hWnd);
void CleanupDeviceD3D();
void CreateRenderTarget();
void CleanupRenderTarget();
LRESULT WINAPI WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Main code
int main(int argc, char** argv)
{
    for (int i = 1; i + 2 < argc + 1; ++i)
    {
        if (strcmp(argv[i], "--background") == 0 && i + 1 < argc)
            g_background_arg = argv[i + 1];
        if (strcmp(argv[i], "--capture") == 0 && i + 2 < argc)
        {
            g_capture_path = argv[i + 1];
            if (strcmp(argv[i + 2], "random") == 0)
                g_capture_random_id = true;
            else
                g_capture_id = (unsigned int)strtoul(argv[i + 2], nullptr, 10);
            if (i + 3 < argc)
                g_capture_strength = (float)atof(argv[i + 3]);
        }
    }
    // Make process DPI aware and obtain main monitor scale
    ImGui_ImplWin32_EnableDpiAwareness();
    float main_scale = ImGui_ImplWin32_GetDpiScaleForMonitor(::MonitorFromPoint(POINT{ 0, 0 }, MONITOR_DEFAULTTOPRIMARY));

    // Create application window
    WNDCLASSEXW wc = { sizeof(wc), CS_CLASSDC, WndProc, 0L, 0L, GetModuleHandle(nullptr), nullptr, nullptr, nullptr, nullptr, L"ImGui Example", nullptr };
    ::RegisterClassExW(&wc);
    HWND hwnd = ::CreateWindowW(wc.lpszClassName, L"Dear ImGui DirectX11 Example", WS_OVERLAPPEDWINDOW, 100, 100, (int)(1280 * main_scale), (int)(800 * main_scale), nullptr, nullptr, wc.hInstance, nullptr);

    // Initialize Direct3D
    if (!CreateDeviceD3D(hwnd))
    {
        CleanupDeviceD3D();
        ::UnregisterClassW(wc.lpszClassName, wc.hInstance);
        return 1;
    }

    // Show the window
    ::ShowWindow(hwnd, SW_SHOWDEFAULT);
    ::UpdateWindow(hwnd);

    // Setup Dear ImGui context
    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO(); (void)io;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;     // Enable Keyboard Controls
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableGamepad;      // Enable Gamepad Controls

    // Setup Dear ImGui style
    ImGui::StyleColorsDark();
    //ImGui::StyleColorsLight();

    // Setup scaling
    ImGuiStyle& style = ImGui::GetStyle();
    style.ScaleAllSizes(main_scale);        // Bake a fixed style scale. (until we have a solution for dynamic style scaling, changing this requires resetting Style + calling this again)
    style.FontScaleDpi = main_scale;        // Set initial font scale. (in docking branch: using io.ConfigDpiScaleFonts=true automatically overrides this for every window depending on the current monitor)

    // Setup Platform/Renderer backends
    ImGui_ImplWin32_Init(hwnd);
    ImGui_ImplDX11_Init(g_pd3dDevice, g_pd3dDeviceContext);

    // Watermark embedder. Sized to the initial client area; kept in step with
    // the swap chain by the resize handler below.
    RECT client_rect;
    ::GetClientRect(hwnd, &client_rect);
    bool watermark_ready = g_watermark.Initialise(g_pd3dDevice, g_pd3dDeviceContext)
                        && g_watermark.ResizeBuffers(client_rect.right - client_rect.left,
                                                     client_rect.bottom - client_rect.top);
    unsigned int match_id = (g_capture_path && !g_capture_random_id) ? g_capture_id : RandomMatchId();
    // Report the ID in use, so a capture run can be checked against it and a
    // live run tells the operator which ID this session is stamping.
    printf("match ID: %u\n", match_id);
    fflush(stdout);
    g_watermark.SetPayload(match_id);
    if (g_capture_strength >= 0.0f)
        g_watermark.SetStrength(g_capture_strength);

    bool show_background = ResolveBackgroundPath()
        && LoadBackgroundTexture(g_pd3dDevice, g_background_path, &g_backgroundView, &g_backgroundWidth, &g_backgroundHeight);

    // Load Fonts
    // - If fonts are not explicitly loaded, Dear ImGui will select an embedded font: either AddFontDefaultVector() or AddFontDefaultBitmap().
    //   This selection is based on (style.FontSizeBase * style.FontScaleMain * style.FontScaleDpi) reaching a small threshold.
    // - You can load multiple fonts and use ImGui::PushFont()/PopFont() to select them.
    // - If a file cannot be loaded, AddFont functions will return a nullptr. Please handle those errors in your code (e.g. use an assertion, display an error and quit).
    // - Read 'docs/FONTS.md' for more instructions and details.
    // - Use '#define IMGUI_ENABLE_FREETYPE' in your imconfig file to use FreeType for higher quality font rendering.
    // - Remember that in C/C++ if you want to include a backslash \ in a string literal you need to write a double backslash \\ !
    //style.FontSizeBase = 20.0f;
    //io.Fonts->AddFontDefaultVector();
    //io.Fonts->AddFontDefaultBitmap();
    //io.Fonts->AddFontFromFileTTF("c:\\Windows\\Fonts\\segoeui.ttf");
    //io.Fonts->AddFontFromFileTTF("../../misc/fonts/DroidSans.ttf");
    //io.Fonts->AddFontFromFileTTF("../../misc/fonts/Roboto-Medium.ttf");
    //io.Fonts->AddFontFromFileTTF("../../misc/fonts/Cousine-Regular.ttf");
    //ImFont* font = io.Fonts->AddFontFromFileTTF("c:\\Windows\\Fonts\\ArialUni.ttf");
    //IM_ASSERT(font != nullptr);

    // Our state
    bool show_demo_window = true;
    bool show_another_window = false;
    bool show_watermark_window = false;
    ImVec4 clear_color = ImVec4(0.45f, 0.55f, 0.60f, 1.00f);

    // Main loop
    bool done = false;
    while (!done)
    {
        // Poll and handle messages (inputs, window resize, etc.)
        // See the WndProc() function below for our to dispatch events to the Win32 backend.
        MSG msg;
        while (::PeekMessage(&msg, nullptr, 0U, 0U, PM_REMOVE))
        {
            ::TranslateMessage(&msg);
            ::DispatchMessage(&msg);
            if (msg.message == WM_QUIT)
                done = true;
        }
        if (done)
            break;

        // Handle window being minimized or screen locked
        if (g_SwapChainOccluded && g_pSwapChain->Present(0, DXGI_PRESENT_TEST) == DXGI_STATUS_OCCLUDED)
        {
            ::Sleep(10);
            continue;
        }
        g_SwapChainOccluded = false;

        // Handle window resize (we don't resize directly in the WM_SIZE handler)
        if (g_ResizeWidth != 0 && g_ResizeHeight != 0)
        {
            CleanupRenderTarget();
            g_pSwapChain->ResizeBuffers(0, g_ResizeWidth, g_ResizeHeight, DXGI_FORMAT_UNKNOWN, 0);
            g_watermark.ResizeBuffers(g_ResizeWidth, g_ResizeHeight);
            g_ResizeWidth = g_ResizeHeight = 0;
            CreateRenderTarget();
        }

        // Start the Dear ImGui frame
        ImGui_ImplDX11_NewFrame();
        ImGui_ImplWin32_NewFrame();
        ImGui::NewFrame();
        DrawBackground(show_background);

        // 1. Show the big demo window (Most of the sample code is in ImGui::ShowDemoWindow()! You can browse its code to learn more about Dear ImGui!).
        if (show_demo_window)
            ImGui::ShowDemoWindow(&show_demo_window);

        // 2. Show a simple window that we create ourselves. We use a Begin/End pair to create a named window.
        {
            static float f = 0.0f;
            static int counter = 0;

            ImGui::Begin("Hello, world!");                          // Create a window called "Hello, world!" and append into it.

            ImGui::Text("This is some useful text.");               // Display some text (you can use a format strings too)
            ImGui::Checkbox("Demo Window", &show_demo_window);      // Edit bools storing our window open/close state
            ImGui::Checkbox("Another Window", &show_another_window);
            ImGui::Checkbox("Watermark", &show_watermark_window);

            ImGui::SliderFloat("float", &f, 0.0f, 1.0f);            // Edit 1 float using a slider from 0.0f to 1.0f
            ImGui::ColorEdit3("clear color", (float*)&clear_color); // Edit 3 floats representing a color

            if (ImGui::Button("Button"))                            // Buttons return true when clicked (most widgets return true when edited/activated)
                counter++;
            ImGui::SameLine();
            ImGui::Text("counter = %d", counter);

            ImGui::Text("Application average %.3f ms/frame (%.1f FPS)", 1000.0f / io.Framerate, io.Framerate);
            ImGui::End();
        }

        // 3. Watermark controls. Strength is the tradeoff between how well the
        // mark survives capture and how visible it is on flat backgrounds; the
        // usable value has to be found by recording the window and decoding.
        if (show_watermark_window)
        {
            ImGui::Begin("Watermark", &show_watermark_window);
            bool enabled = g_watermark.Enabled();
            if (ImGui::Checkbox("Enabled", &enabled))
                g_watermark.SetEnabled(enabled);

            // The payload is a full 32-bit unsigned value, so it is edited as
            // one rather than through InputInt's signed int.
            if (ImGui::InputScalar("Match ID", ImGuiDataType_U32, &match_id, nullptr, nullptr, "%u"))
                g_watermark.SetPayload(match_id);
            ImGui::SameLine();
            ImGui::TextDisabled("(?)");
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("The 32-bit ID carried by the mark. Changing it takes effect "
                                  "on the next frame; the decoder reports this value.");

            float strength = g_watermark.Strength();
            if (ImGui::SliderFloat("Strength", &strength, 0.0f, 0.20f, "%.4f"))
                g_watermark.SetStrength(strength);

            bool alternate = g_watermark.AlternatePolarity();
            if (ImGui::Checkbox("Alternate polarity (experimental)", &alternate))
                g_watermark.SetAlternatePolarity(alternate);
            ImGui::SameLine();
            ImGui::TextDisabled("(?)");
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Flips the pattern's sign every 25 ms so a live viewer sees "
                                  "even less of it. Camera exposures of 1/30 s or longer "
                                  "cancel the mark; screen recordings are unaffected.");

            ImGui::BeginDisabled(g_backgroundView == nullptr);
            ImGui::Checkbox("Show background", &show_background);
            ImGui::EndDisabled();
            if (g_backgroundView)
            {
                const char* name = strrchr(g_background_path, '\\');
                ImGui::Text("Backdrop: %s (%dx%d)", name ? name + 1 : g_background_path, g_backgroundWidth, g_backgroundHeight);
            }
            else
                ImGui::TextUnformatted("No backdrop image found (use --background <file>).");

            ImGui::Separator();
            ImGui::TextUnformatted("Raise strength until the decoder recovers");
            ImGui::TextUnformatted("the ID from your capture, then back off.");
            if (!watermark_ready)
                ImGui::TextUnformatted("Watermark failed to initialise.");
            ImGui::End();
        }

        // 4. Show another simple window.
        if (show_another_window)
        {
            ImGui::Begin("Another Window", &show_another_window);   // Pass a pointer to our bool variable (the window will have a closing button that will clear the bool when clicked)
            ImGui::Text("Hello from another window!");
            if (ImGui::Button("Close Me"))
                show_another_window = false;
            ImGui::End();
        }

        // Rendering
        ImGui::Render();
        const float clear_color_with_alpha[4] = { clear_color.x * clear_color.w, clear_color.y * clear_color.w, clear_color.z * clear_color.w, clear_color.w };
        // With the watermark active the frame is drawn into an offscreen
        // target first, because the shader has to read the finished frame in
        // order to modulate it, and a resource cannot be bound for reading and
        // writing at the same time.
        const bool use_watermark = watermark_ready && g_watermark.Enabled();
        ID3D11RenderTargetView* frame_target = use_watermark ? g_watermark.SceneTarget() : g_mainRenderTargetView;
        g_pd3dDeviceContext->OMSetRenderTargets(1, &frame_target, nullptr);
        g_pd3dDeviceContext->ClearRenderTargetView(frame_target, clear_color_with_alpha);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        if (use_watermark)
            g_watermark.Apply(g_mainRenderTargetView);

        // Present
        HRESULT hr = g_pSwapChain->Present(1, 0);   // Present with vsync
        //HRESULT hr = g_pSwapChain->Present(0, 0); // Present without vsync
        g_SwapChainOccluded = (hr == DXGI_STATUS_OCCLUDED);

        if (g_capture_path && ++g_capture_frames >= 8)
        {
            SaveBackbufferPPM(g_capture_path);
            break;
        }
    }

    // Cleanup
    if (g_backgroundView) { g_backgroundView->Release(); g_backgroundView = nullptr; }
    g_watermark.Release();
    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();

    CleanupDeviceD3D();
    ::DestroyWindow(hwnd);
    ::UnregisterClassW(wc.lpszClassName, wc.hInstance);

    return 0;
}

// Helper functions

bool CreateDeviceD3D(HWND hWnd)
{
    // Setup swap chain
    // This is a basic setup. Optimally could use e.g. DXGI_SWAP_EFFECT_FLIP_DISCARD and handle fullscreen mode differently. See #8979 for suggestions.
    DXGI_SWAP_CHAIN_DESC sd;
    ZeroMemory(&sd, sizeof(sd));
    sd.BufferCount = 2;
    sd.BufferDesc.Width = 0;
    sd.BufferDesc.Height = 0;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferDesc.RefreshRate.Numerator = 60;
    sd.BufferDesc.RefreshRate.Denominator = 1;
    sd.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = hWnd;
    sd.SampleDesc.Count = 1;
    sd.SampleDesc.Quality = 0;
    sd.Windowed = TRUE;
    sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    UINT createDeviceFlags = 0;
    //createDeviceFlags |= D3D11_CREATE_DEVICE_DEBUG;
    D3D_FEATURE_LEVEL featureLevel;
    const D3D_FEATURE_LEVEL featureLevelArray[2] = { D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0, };
    HRESULT res = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, createDeviceFlags, featureLevelArray, 2, D3D11_SDK_VERSION, &sd, &g_pSwapChain, &g_pd3dDevice, &featureLevel, &g_pd3dDeviceContext);
    if (res == DXGI_ERROR_UNSUPPORTED) // Try high-performance WARP software driver if hardware is not available.
        res = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, createDeviceFlags, featureLevelArray, 2, D3D11_SDK_VERSION, &sd, &g_pSwapChain, &g_pd3dDevice, &featureLevel, &g_pd3dDeviceContext);
    if (res != S_OK)
        return false;

    CreateRenderTarget();
    return true;
}

void CleanupDeviceD3D()
{
    CleanupRenderTarget();
    if (g_pSwapChain) { g_pSwapChain->Release(); g_pSwapChain = nullptr; }
    if (g_pd3dDeviceContext) { g_pd3dDeviceContext->Release(); g_pd3dDeviceContext = nullptr; }
    if (g_pd3dDevice) { g_pd3dDevice->Release(); g_pd3dDevice = nullptr; }
}

void CreateRenderTarget()
{
    ID3D11Texture2D* pBackBuffer;
    g_pSwapChain->GetBuffer(0, IID_PPV_ARGS(&pBackBuffer));
    g_pd3dDevice->CreateRenderTargetView(pBackBuffer, nullptr, &g_mainRenderTargetView);
    pBackBuffer->Release();
}

void CleanupRenderTarget()
{
    if (g_mainRenderTargetView) { g_mainRenderTargetView->Release(); g_mainRenderTargetView = nullptr; }
}

// Forward declare message handler from imgui_impl_win32.cpp
extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Win32 message handler
// You can read the io.WantCaptureMouse, io.WantCaptureKeyboard flags to tell if dear imgui wants to use your inputs.
// - When io.WantCaptureMouse is true, do not dispatch mouse input data to your main application, or clear/overwrite your copy of the mouse data.
// - When io.WantCaptureKeyboard is true, do not dispatch keyboard input data to your main application, or clear/overwrite your copy of the keyboard data.
// Generally you may always pass all inputs to dear imgui, and hide them from your application based on those two flags.
LRESULT WINAPI WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam)
{
    if (ImGui_ImplWin32_WndProcHandler(hWnd, msg, wParam, lParam))
        return true;

    switch (msg)
    {
    case WM_SIZE:
        if (wParam == SIZE_MINIMIZED)
            return 0;
        g_ResizeWidth = (UINT)LOWORD(lParam); // Queue resize
        g_ResizeHeight = (UINT)HIWORD(lParam);
        return 0;
    case WM_SYSCOMMAND:
        if ((wParam & 0xfff0) == SC_KEYMENU) // Disable ALT application menu
            return 0;
        break;
    case WM_DESTROY:
        ::PostQuitMessage(0);
        return 0;
    }
    return ::DefWindowProcW(hWnd, msg, wParam, lParam);
}
