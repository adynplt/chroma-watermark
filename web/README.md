# Chroma watermark in the browser

The [desktop embedder](../desktop) as a WebGL pass, so a
web page can stamp a 32-bit ID into an image, a video or a canvas it draws.
Same key, layout, profile and perceptual weight as the Direct3D version, so
the repository's Python decoder reads a browser screenshot unchanged.

**`index.html` is the whole demo in one self-contained file.** Open it from
disk or from any server; it needs nothing else. It has two views, switched
from a link in each:

- **Website view (the default).** An ordinary-looking page, "Aurora Studio":
  navigation, headline, copy, a feature row, a footer. Only the hero image
  carries the mark. The nav has a Watermark checkbox and the ID, shown
  blurred until it is hovered or clicked; type a number there and press Enter
  to change it.
- **Full-screen view.** The backdrop image covers the window and carries the
  mark. A panel sets the ID and strength, toggles the mark, collapses out of
  the way (button or the H key), and downloads the marked canvas as a PNG.

Both views stamp the same ID. The URL parameters are read once at load and
never rewritten; changing the ID or the view on the page leaves the address
bar alone.

```
index.html      the built, self-contained demo (do not edit; see app.html)
app.html        the demo's source, referencing watermark.js and assets/
watermark.js    the embedder, no dependencies
assets/         background.png (full-screen view), hero.png (website view)
tools/build.py  bundles app.html + watermark.js + images into index.html
tools/verify.py renders the pages headless in Chrome and decodes what they show
```

## Try it

Open `index.html`. Take a screenshot and decode it with the repository's
decoder:

```bash
python ../tools/decode_watermark.py shot.png --expect 987654321
```

For the website view, the mark is only in the hero image, so give the
decoder the element's rectangle. The page writes it to
`document.body.dataset.markRect` as `x,y,w,h` in device pixels (read it from
the console), and either an approximate crop with the search on or a loose
box around the element works:

```bash
python ../tools/decode_watermark.py shot.png --expect 987654321 --crop 570 118 630 395
python ../tools/decode_watermark.py shot.png --expect 987654321 --box 540 90 680 450
```

URL parameters: `view=site|full` (default site), `id`, `strength` (default 0.08),
`fit=native` (full-screen view at the image's own pixel size), `dpr`
(override the device pixel ratio), `options=0` (panel collapsed), `panel=0`
(no panel, for automated screenshots).

## Editing

Edit `app.html`, `watermark.js` or the images, then rebuild:

```bash
python tools/build.py      # writes index.html
python tools/verify.py     # renders and decodes, needs Chrome; uses ../tools/decode_watermark.py
```

`app.html` also runs directly, but only over HTTP (`python -m http.server`),
because WebGL will not read an image from `file://`. The built `index.html`
has the images inlined, which is why it works from anywhere.

## Use it on your own page

Mark one `<img>` in an existing page. The call replaces it with a canvas of
the same layout that shows the marked image, and keeps the canvas at one
device pixel per canvas pixel as the page reflows:

```js
const mark = ChromaWatermark.attachToImage(document.querySelector("img.hero"), {
    payload: sessionId,      // 32-bit unsigned
    strength: 0.08,          // optional
});
mark.setPayload(other);      // later: change the ID, toggle, and so on
mark.setEnabled(false);
mark.render();
```

Or drive the embedder yourself, for a canvas or video you already draw:

```js
const mark = new ChromaWatermark(canvas);
mark.setSource(imageOrVideo);          // <img>, <video>, <canvas>, ImageBitmap
mark.setPayload(sessionId);
mark.resize(width, height);            // device pixels
mark.render();                         // once for an image, per frame for video
```

Two rules matter. One canvas pixel must be one device pixel: size the canvas
to `cssWidth * devicePixelRatio` and display it at `cssWidth` CSS pixels, or
the browser resamples the mark on the way to the screen. And the source is
centre-cropped to cover the canvas, so the perceptual weight is computed on
the pixels that actually reach the screen.

## Verified

`tools/verify.py` renders the pages in headless Chrome and decodes the
screenshots with the repository's decoder:

- Pixel-exact render of the full-screen view at the image's own size,
  compared against the repository's numpy model of the shader: mean
  difference 0.1 of 255, maximum 1.
- `index.html` opened from `file://`, full-screen view, 1280×800 with the
  panel showing, at device pixel ratio 1 and 2.
- `index.html` opened from `file://`, website view at 1280×900, the hero
  image a 621×388 element of the screenshot, decoded from a crop with the
  search on and from a loose box.
- Strength 0 control, which must not read as a mark.

It needs Chrome. Headless Edge on Windows 11 exits without writing a
screenshot, so it is tried last.

## What this cannot do

- **Mark arbitrary DOM content.** JavaScript cannot read the rendered pixels
  of a div, so the perceptual weight cannot be computed for it. Only content
  the page draws itself (images, video, canvas, WebGL) can be marked
  adaptively. An overlay canvas with `mix-blend-mode` can add a fixed,
  non-adaptive pattern over a div; it is more visible on flat areas and the
  decoder would need a uniform-weight option.
- **Resist a user who edits the page.** Anything embedded client-side is
  removed by deleting the canvas or saving the original image URL. For images
  the strong option is embedding on the server with the Python code, so the
  marked pixels are the only pixels the client ever receives. The WebGL pass
  is the right tool when the content is generated in the browser and the goal
  is attributing screenshots and recordings.
- **White pages.** The weight gives flat white 30% of full strength, about 6
  of 255 on blue at the default, which is faintly visible as tiles. Lower the
  strength for mostly-white content.
- **Small elements.** At 621 px wide the cells are about 19 px and the mark
  decodes with a healthy margin; a thumbnail would not.
