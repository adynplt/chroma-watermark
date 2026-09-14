"""Bundle app.html into the self-contained index.html.

Inlines watermark.js and embeds both images as data URIs, so index.html opens
from anywhere, including a file:// path, with no server and no other files.
The images are re-encoded as JPEG to keep the file small; that changes
nothing about the mark, which is embedded live in the browser.

    python tools/build.py            -> index.html
"""

import base64
import os
import re
import sys

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
JPEG_QUALITY = 92
IMAGES = ["assets/background.png", "assets/hero.png"]


def data_uri(path):
    image = cv2.imread(os.path.join(SITE, path))
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError(f"could not encode {path}")
    return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"), len(encoded)


def main():
    source = open(os.path.join(SITE, "app.html"), encoding="utf-8").read()
    script = open(os.path.join(SITE, "watermark.js"), encoding="utf-8").read()

    # "</script>" inside the inlined source would end the tag early.
    script = script.replace("</script>", "<\\/script>")
    bundled, count = re.subn(r'<script src="watermark\.js"></script>',
                             lambda _: "<script>\n" + script + "\n</script>", source)
    if count != 1:
        print("app.html does not reference watermark.js exactly once", file=sys.stderr)
        return 1

    sizes = []
    for path in IMAGES:
        uri, size = data_uri(path)
        bundled, count = re.subn(re.escape(f'"{path}"'), lambda _: f'"{uri}"', bundled)
        if count != 1:
            print(f"app.html does not reference {path} exactly once", file=sys.stderr)
            return 1
        sizes.append(f"{os.path.basename(path)} {size / 1024:.0f} KB")

    output = os.path.join(SITE, "index.html")
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(bundled)
    print(f"{output}: {os.path.getsize(output) / 1024:.0f} KB ({', '.join(sizes)} as JPEG q{JPEG_QUALITY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
