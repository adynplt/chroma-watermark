"""Render the demo in a headless browser and decode what it shows.

Opens the pages headless in Chrome, takes screenshots, and decodes them with
the repository's Python decoder in ../tools:

  1. app.html over HTTP, full-screen view, the canvas at the image's own
     size, controls hidden. Compared pixel for pixel against the repository's
     numpy model of the shader, which shows the WebGL port embeds the same
     mark. (app.html is used because it loads the lossless PNG; index.html
     carries JPEG copies.)
  2. index.html from a file:// path, full-screen view, a 1280x800 window
     with the controls showing, at device pixel ratio 1 and 2.
  3. index.html from a file:// path, website view, 1280x900: the marked hero
     image is one element of the page. Decoded from the element's rectangle
     as the page reports it, with the geometry search on, and from a loose
     box around it.
  4. Strength 0 control, which must not read as a mark.

    python tools/verify.py [--id N] [--strength S]

Run tools/build.py first so index.html is current. Exit code 0 when every
render decodes to the expected ID.
"""

import argparse
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)

# Chrome first: on at least one Windows 11 machine headless Edge exits 0
# without ever writing the screenshot.
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser(explicit):
    candidates = [explicit] if explicit else []
    candidates += BROWSERS + [shutil.which("chrome"), shutil.which("chromium"), shutil.which("msedge")]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def serve(directory):
    """Serve `directory` on a free localhost port in a daemon thread."""
    handler_class = type("QuietHandler", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), lambda *a, **k: handler_class(*a, directory=directory, **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def browser_command(browser, url, width, height, dpr, profile_dir):
    return [
        browser, "--headless=new", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--mute-audio",
        f"--user-data-dir={profile_dir}",
        f"--window-size={width},{height}",
        f"--force-device-scale-factor={dpr}",
        # Let the images load and the frame render before the capture.
        "--virtual-time-budget=10000",
        url,
    ]


def screenshot(browser, url, path, width, height, dpr, profile_dir):
    command = browser_command(browser, url, width, height, dpr, profile_dir)
    command.insert(-1, f"--screenshot={path}")
    subprocess.run(command, check=True, timeout=120, capture_output=True)
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"{os.path.basename(browser)} wrote no screenshot; try --browser with Chrome")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float64) / 255.0


def dump_dom(browser, url, width, height, dpr, profile_dir):
    command = browser_command(browser, url, width, height, dpr, profile_dir)
    command.insert(-1, "--dump-dom")
    return subprocess.run(command, check=True, timeout=120, capture_output=True, text=True).stdout


def describe(result):
    return (f"presence {result['presence']['cross_validated']:.3f}  "
            f"weakest bit {np.abs(result['correlation']).min():.4f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.path.join(SITE, ".."),
                        help="repository root, whose tools/ holds the decoder (default: the parent folder)")
    parser.add_argument("--id", type=int, default=987654321)
    parser.add_argument("--strength", type=float, default=0.08)
    parser.add_argument("--browser", default=None, help="path to chrome.exe or msedge.exe")
    parser.add_argument("--keep", default=None, help="directory to keep the screenshots in")
    args = parser.parse_args()

    tools = os.path.join(os.path.abspath(args.repo), "tools")
    if not os.path.exists(os.path.join(tools, "decode_watermark.py")):
        print(f"decoder not found under {tools}; pass --repo", file=sys.stderr)
        return 2
    sys.path.insert(0, tools)
    from decode_watermark import analyse_frames  # noqa: E402
    from test_roundtrip import embed  # noqa: E402

    browser = find_browser(args.browser)
    if not browser:
        print("no Chrome or Edge found; pass --browser", file=sys.stderr)
        return 2
    if not os.path.exists(os.path.join(SITE, "index.html")):
        print("index.html is missing; run tools/build.py first", file=sys.stderr)
        return 2

    server, port = serve(SITE)
    app = f"http://127.0.0.1:{port}/app.html?id={args.id}&strength={args.strength}"
    single = "file:///" + os.path.join(SITE, "index.html").replace("\\", "/") + f"?id={args.id}&strength={args.strength}"
    out_dir = args.keep or tempfile.mkdtemp(prefix="chroma-web-")
    os.makedirs(out_dir, exist_ok=True)
    profile_dir = os.path.join(out_dir, "profile")
    failures = 0

    def check(label, result, expected):
        nonlocal failures
        wrong = bin(result["payload"] ^ expected).count("1")
        ok = wrong == 0 and result["crc_ok"] and result["present"]
        failures += not ok
        print(f"  {label:26s} decoded {result['payload']:10d}  {describe(result)}  "
              f"{'ok' if ok else f'FAIL ({wrong} bits wrong)'}")

    try:
        source = cv2.cvtColor(cv2.imread(os.path.join(SITE, "assets", "background.png")), cv2.COLOR_BGR2RGB)
        source = source.astype(np.float64) / 255.0
        height, width = source.shape[:2]

        # 1. Pixel-exact render against the numpy model.
        print(f"pixel-exact render, app.html full-screen view, {width}x{height}, ID {args.id}, strength {args.strength}")
        path = os.path.join(out_dir, "native.png")
        frame = screenshot(browser, app + "&view=full&fit=native&dpr=1&panel=0", path, width, height, 1, profile_dir)
        if frame.shape[:2] != (height, width):
            print(f"  screenshot is {frame.shape[1]}x{frame.shape[0]}, expected {width}x{height}")
            failures += 1
        else:
            model = embed(source, args.id, args.strength)
            web_mark = (frame - source)[..., 2] - (frame - source)[..., 0]
            model_mark = (model - source)[..., 2] - (model - source)[..., 0]
            difference = np.abs(frame - model) * 255.0
            correlation = np.corrcoef(web_mark.ravel(), model_mark.ravel())[0, 1]
            print(f"  against the numpy shader model: mean |diff| {difference.mean():.3f}/255, "
                  f"max {difference.max():.0f}/255, mark correlation {correlation:.4f}")
            check("no geometry search", analyse_frames([frame], sync="off"), args.id)

        # 2. The single file from disk, full-screen view, controls showing.
        for dpr in (1, 2):
            print(f"\nindex.html from file://, full-screen view, 1280x800 CSS pixels at device pixel ratio {dpr}")
            path = os.path.join(out_dir, f"full_dpr{dpr}.png")
            frame = screenshot(browser, single + f"&view=full&dpr={dpr}", path, 1280, 800, dpr, profile_dir)
            check(f"{frame.shape[1]}x{frame.shape[0]} screenshot", analyse_frames([frame], sync="refine"), args.id)

        # 3. The single file from disk, website view: one element is marked.
        # The page reports the element's rectangle; a headless layout can
        # shift a few pixels between the DOM dump and the screenshot, so the
        # crop gets a margin and the geometry search, as a hand-made crop
        # would need anyway. The loose box is what a person would give.
        print("\nindex.html from file://, website view, 1280x900, hero image marked")
        url = single + "&view=site&dpr=1"
        dom = dump_dom(browser, url, 1280, 900, 1, profile_dir)
        match = re.search(r'data-mark-rect="([^"]+)"', dom)
        error = re.search(r'data-error="([^"]+)"', dom)
        if error or not match:
            print(f"  page did not report the marked element: {error.group(1) if error else 'no rect'}")
            failures += 1
        else:
            x, y, w, h = (int(v) for v in match.group(1).split(","))
            path = os.path.join(out_dir, "site.png")
            frame = screenshot(browser, url, path, 1280, 900, 1, profile_dir)
            print(f"  marked element reported at x {x} y {y}, {w}x{h} of a {frame.shape[1]}x{frame.shape[0]} screenshot")
            margin = 8
            crop = frame[max(0, y - margin):y + h + margin, max(0, x - margin):x + w + margin]
            check("element crop, searched", analyse_frames([crop], sync="auto"), args.id)
            check("loose box on the page", analyse_frames([frame], box=(x - 30, y - 30, w + 60, h + 60)), args.id)

        # 4. Control: strength 0 must not read as a mark.
        print("\nstrength 0 control")
        path = os.path.join(out_dir, "control.png")
        frame = screenshot(browser, f"http://127.0.0.1:{port}/app.html?id={args.id}&strength=0&view=full&fit=native&dpr=1&panel=0",
                           path, width, height, 1, profile_dir)
        result = analyse_frames([frame], sync="off")
        ok = not result["present"]
        failures += not ok
        print(f"  {describe(result)}  {'rejected, ok' if ok else 'FAIL: accepted as a mark'}")
    finally:
        server.shutdown()
        if args.keep:
            print(f"\nscreenshots kept in {out_dir}")
        else:
            shutil.rmtree(out_dir, ignore_errors=True)

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall renders decoded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
