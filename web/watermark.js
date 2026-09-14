// Chroma watermark embedder for the browser, WebGL 1.
//
// A port of the Direct3D 11 pass in ../desktop/src/watermark.cpp: the same
// key, cell layout, raised-cosine profile, blue-yellow axis and perceptual
// weight, so the repository's Python decoder reads the output unchanged.
//
// Usage:
//     const mark = new ChromaWatermark(canvas);
//     mark.setSource(image);          // an <img>, <video> or another canvas
//     mark.setPayload(1234567);
//     mark.resize(width, height);     // device pixels
//     mark.render();                  // once for an image, per frame for video
//
// The canvas must be displayed at exactly `width / devicePixelRatio` CSS
// pixels, or the browser resamples the mark on the way to the screen.

"use strict";

const WATERMARK_KEY = 0x5A17C0DE;

const LAYOUT = Object.freeze({
    payloadBits: 32,
    crcBits: 8,
    codedBits: 40,
    repeatPerBit: 22,
    dataCells: 880,      // codedBits * repeatPerBit
    pilotCells: 144,
    gridCols: 32,
    gridRows: 32,
    totalCells: 1024,    // gridCols * gridRows
});
if (LAYOUT.codedBits !== LAYOUT.payloadBits + LAYOUT.crcBits
    || LAYOUT.dataCells !== LAYOUT.codedBits * LAYOUT.repeatPerBit
    || LAYOUT.totalCells !== LAYOUT.gridCols * LAYOUT.gridRows
    || LAYOUT.dataCells + LAYOUT.pilotCells !== LAYOUT.totalCells)
    throw new Error("watermark layout constants are inconsistent");

// Perceptual weight parameters; must match kMask* in watermark.cpp and
// MASK_* in decode_watermark.py.
const MASK = Object.freeze({ knee: 0.30, floor: 0.30, textureLow: 0.010, textureHigh: 0.040, radiusCells: 0.06 });

// ---- Pattern generation, bit-identical to the C++ and Python -----------------

// Integer avalanche hash on unsigned 32-bit values. Math.imul keeps the
// multiplications in 32 bits; ">>> 0" keeps every intermediate unsigned.
function hashU32(x) {
    x = x >>> 0;
    x = (x ^ (x >>> 16)) >>> 0;
    x = Math.imul(x, 0x7FEB352D) >>> 0;
    x = (x ^ (x >>> 15)) >>> 0;
    x = Math.imul(x, 0x846CA68B) >>> 0;
    x = (x ^ (x >>> 16)) >>> 0;
    return x;
}

// CRC-8, polynomial 0x07, no reflection, zero init.
function crc8(value) {
    let crc = 0;
    for (let byte = 0; byte < 4; ++byte) {
        crc ^= (value >>> (8 * byte)) & 0xFF;
        for (let bit = 0; bit < 8; ++bit)
            crc = (crc & 0x80) ? ((crc << 1) ^ 0x07) & 0xFF : (crc << 1) & 0xFF;
    }
    return crc;
}

// Which coded bit a cell carries and with which sign; pilots have bit -1.
function describeCell(cellIndex) {
    if (cellIndex >= LAYOUT.dataCells) {
        const h = hashU32(WATERMARK_KEY ^ hashU32((cellIndex + 0x50110000) >>> 0));
        return { bitIndex: -1, sign: (h & 1) ? -1 : 1 };
    }
    const bitIndex = Math.floor(cellIndex / LAYOUT.repeatPerBit);
    const copyIndex = cellIndex % LAYOUT.repeatPerBit;
    const h = hashU32(WATERMARK_KEY ^ hashU32(bitIndex));
    const secondHalf = copyIndex >= LAYOUT.repeatPerBit / 2;
    const flip = (h & 1) !== 0;
    return { bitIndex, sign: (secondHalf !== flip) ? -1 : 1 };
}

// Keyed Fisher-Yates permutation of cell positions.
let positionTable = null;
function cellPositionTable() {
    if (positionTable)
        return positionTable;
    const table = new Int32Array(LAYOUT.totalCells);
    for (let i = 0; i < LAYOUT.totalCells; ++i)
        table[i] = i;
    for (let i = LAYOUT.totalCells - 1; i > 0; --i) {
        const j = hashU32((WATERMARK_KEY + 0x9E3779B9 + i) >>> 0) % (i + 1);
        const swap = table[i];
        table[i] = table[j];
        table[j] = swap;
    }
    positionTable = table;
    return table;
}

// The signed amplitude of every grid position for a payload, as a 32x32
// array in row-major order, values +1 or -1.
function amplitudeGrid(payload) {
    payload = payload >>> 0;
    const crc = crc8(payload);
    const grid = new Float32Array(LAYOUT.totalCells);
    const positions = cellPositionTable();
    for (let cell = 0; cell < LAYOUT.totalCells; ++cell) {
        const { bitIndex, sign } = describeCell(cell);
        let amplitude = sign;
        if (bitIndex >= 0) {
            const bitSet = bitIndex < LAYOUT.payloadBits
                ? (payload >>> bitIndex) & 1
                : (crc >>> (bitIndex - LAYOUT.payloadBits)) & 1;
            amplitude = bitSet ? sign : -sign;
        }
        grid[positions[cell]] = amplitude;
    }
    return grid;
}

// ---- Shaders -------------------------------------------------------------------

// Full-screen triangle. uv has (0, 0) at the top-left of the canvas, like the
// Direct3D version, so cell (0, 0) is the top-left cell.
const VERTEX_SHADER = `
attribute vec2 position;
varying vec2 vUv;
void main() {
    vUv = vec2(position.x * 0.5 + 0.5, 0.5 - position.y * 0.5);
    gl_Position = vec4(position, 0.0, 1.0);
}
`;

// Copies the source into the canvas-sized scene texture with a centre crop,
// so the mark pass reads a texture whose texels are the output pixels.
//
// A framebuffer stores its bottom row at t = 0, while an uploaded image
// stores its top row there. Flipping the lookup here makes the scene
// texture's t = 0 the top of the image, so the mark pass can address both
// the scene and the cell grid with the same top-down uv.
const BLIT_SHADER = `
precision highp float;
uniform sampler2D source;
uniform vec2 fit;
varying vec2 vUv;
void main() {
    vec2 uv = vec2(vUv.x, 1.0 - vUv.y);
    gl_FragColor = vec4(texture2D(source, (uv - 0.5) * fit + 0.5).rgb, 1.0);
}
`;

// The embedding pass. Line for line the HLSL in watermark.cpp; see the
// comments there for the design. Values are display-referred codes in 0..1,
// which is what a canvas holds and what the decoder assumes.
const MARK_SHADER = `
precision highp float;
uniform sampler2D scene;
uniform sampler2D cells;
uniform float strength;
uniform float polarity;
uniform vec2 grid;
uniform vec4 texel;       // xy: one texel in uv units, z: tap radius in texels
uniform vec4 maskParams;  // knee, floor, texture low, texture high
varying vec2 vUv;

const vec3 kLumaWeights = vec3(0.299, 0.587, 0.114);
const vec3 kChromaAxis = vec3(-0.12867, -0.12867, 1.0);

float luma(vec2 uv) {
    return dot(texture2D(scene, uv).rgb, kLumaWeights);
}

float activity(vec2 uv, vec2 r) {
    float centre = luma(uv);
    float total = 0.0;
    total += abs(luma(uv + vec2( r.x,  0.0)) - centre);
    total += abs(luma(uv + vec2(-r.x,  0.0)) - centre);
    total += abs(luma(uv + vec2( 0.0,  r.y)) - centre);
    total += abs(luma(uv + vec2( 0.0, -r.y)) - centre);
    total += abs(luma(uv + vec2( r.x,  r.y)) - centre);
    total += abs(luma(uv + vec2( r.x, -r.y)) - centre);
    total += abs(luma(uv + vec2(-r.x,  r.y)) - centre);
    total += abs(luma(uv + vec2(-r.x, -r.y)) - centre);
    return total / 8.0;
}

void main() {
    vec3 scene3 = texture2D(scene, vUv).rgb;

    vec2 cell = clamp(floor(vUv * grid), vec2(0.0), grid - 1.0);
    float amplitude = texture2D(cells, (cell + 0.5) / grid).r * 2.0 - 1.0;

    vec2 cellUV = fract(vUv * grid);
    vec2 s = sin(3.14159265 * cellUV);
    float profile = s.x * s.x * s.y * s.y;

    float lumaWeight = clamp(dot(scene3, kLumaWeights) / maskParams.x, 0.0, 1.0);
    float weight = 0.0;
    if (lumaWeight > 0.0) {
        vec2 r = texel.xy * texel.z;
        float eroded = activity(vUv, r);
        if (eroded > maskParams.z) {
            for (int dy = -1; dy <= 1; ++dy) {
                for (int dx = -1; dx <= 1; ++dx) {
                    if (dx != 0 || dy != 0)
                        eroded = min(eroded, activity(vUv + vec2(float(dx), float(dy)) * 2.0 * r, r));
                }
            }
        }
        float textureMask = smoothstep(maskParams.z, maskParams.w, eroded);
        weight = lumaWeight * (maskParams.y + (1.0 - maskParams.y) * textureMask);
    }

    vec3 offset = kChromaAxis * (amplitude * strength * profile * weight * polarity);

    vec3 positiveRoom = (1.0 - scene3) / max(offset, vec3(1e-6));
    vec3 negativeRoom = scene3 / max(-offset, vec3(1e-6));
    vec3 room = mix(mix(vec3(1e6), negativeRoom, vec3(lessThan(offset, vec3(0.0)))),
                    positiveRoom, vec3(greaterThan(offset, vec3(0.0))));
    float scale = clamp(min(room.r, min(room.g, room.b)), 0.0, 1.0);

    gl_FragColor = vec4(clamp(scene3 + offset * scale, 0.0, 1.0), 1.0);
}
`;

// ---- The embedder -----------------------------------------------------------

class ChromaWatermark {
    constructor(canvas) {
        this.canvas = canvas;
        // No alpha, no premultiplication, no antialiasing: the canvas must
        // hold exactly the values the shader wrote. preserveDrawingBuffer
        // lets toBlob() read the frame back after render().
        const gl = canvas.getContext("webgl", {
            alpha: false, premultipliedAlpha: false, antialias: false,
            preserveDrawingBuffer: true, depth: false, stencil: false,
        });
        if (!gl)
            throw new Error("WebGL is not available");
        this.gl = gl;

        this.blitProgram = this._program(VERTEX_SHADER, BLIT_SHADER);
        this.markProgram = this._program(VERTEX_SHADER, MARK_SHADER);

        const triangle = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, triangle);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
        this.triangle = triangle;

        this.sourceTexture = this._texture(gl.LINEAR);
        this.sceneTexture = this._texture(gl.NEAREST);
        this.cellTexture = this._texture(gl.NEAREST);
        this.framebuffer = gl.createFramebuffer();

        this.source = null;
        this.sourceWidth = 0;
        this.sourceHeight = 0;
        this.sourceIsVideo = false;
        this.width = 0;
        this.height = 0;
        this.payload = 0;
        this.strength = 0.08;
        this.enabled = true;
        this.polarity = 1.0;
        this._uploadCells();
    }

    // An <img>, <video>, <canvas> or ImageBitmap. A video is re-uploaded on
    // every render(); anything else once, here.
    setSource(source) {
        this.source = source;
        this.sourceIsVideo = typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement;
        this._uploadSource();
    }

    setPayload(payload) {
        payload = payload >>> 0;
        if (payload !== this.payload) {
            this.payload = payload;
            this._uploadCells();
        }
    }

    setStrength(strength) { this.strength = strength; }
    setEnabled(enabled) { this.enabled = enabled; }
    setPolarity(polarity) { this.polarity = polarity; }

    // Output size in device pixels.
    resize(width, height) {
        if (width === this.width && height === this.height)
            return;
        const gl = this.gl;
        this.width = width;
        this.height = height;
        this.canvas.width = width;
        this.canvas.height = height;
        gl.bindTexture(gl.TEXTURE_2D, this.sceneTexture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    }

    render() {
        const gl = this.gl;
        if (!this.source || this.width === 0)
            return;
        if (this.sourceIsVideo)
            this._uploadSource();

        // Pass 1: source -> scene texture, centre-cropped to the output.
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.sceneTexture, 0);
        gl.viewport(0, 0, this.width, this.height);
        gl.useProgram(this.blitProgram);
        this._bindTriangle(this.blitProgram);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.sourceTexture);
        gl.uniform1i(gl.getUniformLocation(this.blitProgram, "source"), 0);
        const cover = Math.max(this.width / this.sourceWidth, this.height / this.sourceHeight);
        gl.uniform2f(gl.getUniformLocation(this.blitProgram, "fit"),
                     this.width / (this.sourceWidth * cover), this.height / (this.sourceHeight * cover));
        // Exact copy when the sizes match; the linear filter would return the
        // same texels at their centres anyway, but nearest makes it certain.
        const exact = this.width === this.sourceWidth && this.height === this.sourceHeight;
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, exact ? gl.NEAREST : gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, exact ? gl.NEAREST : gl.LINEAR);
        gl.drawArrays(gl.TRIANGLES, 0, 3);

        // Pass 2: scene texture -> canvas, with the mark.
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, this.width, this.height);
        const program = this.markProgram;
        gl.useProgram(program);
        this._bindTriangle(program);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.sceneTexture);
        gl.uniform1i(gl.getUniformLocation(program, "scene"), 0);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this.cellTexture);
        gl.uniform1i(gl.getUniformLocation(program, "cells"), 1);

        const cellPx = Math.min(this.width, this.height) / LAYOUT.gridCols;
        const radius = Math.max(1, Math.floor(MASK.radiusCells * cellPx + 0.5));
        gl.uniform1f(gl.getUniformLocation(program, "strength"), this.enabled ? this.strength : 0.0);
        gl.uniform1f(gl.getUniformLocation(program, "polarity"), this.polarity);
        gl.uniform2f(gl.getUniformLocation(program, "grid"), LAYOUT.gridCols, LAYOUT.gridRows);
        gl.uniform4f(gl.getUniformLocation(program, "texel"), 1 / this.width, 1 / this.height, radius, 0);
        gl.uniform4f(gl.getUniformLocation(program, "maskParams"), MASK.knee, MASK.floor, MASK.textureLow, MASK.textureHigh);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // The marked frame as a PNG blob, for tests: the analogue of the
    // --capture hook, independent of any screenshot tooling.
    toBlob() {
        return new Promise((resolve) => this.canvas.toBlob(resolve, "image/png"));
    }

    _uploadSource() {
        const gl = this.gl;
        const source = this.source;
        this.sourceWidth = source.videoWidth || source.naturalWidth || source.width;
        this.sourceHeight = source.videoHeight || source.naturalHeight || source.height;
        gl.bindTexture(gl.TEXTURE_2D, this.sourceTexture);
        // Keep the browser from touching the pixel values on the way in:
        // no premultiplication, no colour-space conversion, top row first.
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    }

    // The 32x32 sign table as a texture: 255 for +1, 0 for -1.
    _uploadCells() {
        const gl = this.gl;
        const grid = amplitudeGrid(this.payload);
        const pixels = new Uint8Array(LAYOUT.totalCells * 4);
        for (let i = 0; i < LAYOUT.totalCells; ++i) {
            const value = grid[i] > 0 ? 255 : 0;
            pixels[i * 4] = value;
            pixels[i * 4 + 1] = value;
            pixels[i * 4 + 2] = value;
            pixels[i * 4 + 3] = 255;
        }
        gl.bindTexture(gl.TEXTURE_2D, this.cellTexture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, LAYOUT.gridCols, LAYOUT.gridRows, 0, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    }

    _texture(filter) {
        const gl = this.gl;
        const texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
        return texture;
    }

    _bindTriangle(program) {
        const gl = this.gl;
        gl.bindBuffer(gl.ARRAY_BUFFER, this.triangle);
        const position = gl.getAttribLocation(program, "position");
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    }

    _program(vertexSource, fragmentSource) {
        const gl = this.gl;
        const compile = (type, source) => {
            const shader = gl.createShader(type);
            gl.shaderSource(shader, source);
            gl.compileShader(shader);
            if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
                throw new Error("shader compile failed: " + gl.getShaderInfoLog(shader));
            return shader;
        };
        const program = gl.createProgram();
        gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource));
        gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource));
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS))
            throw new Error("program link failed: " + gl.getProgramInfoLog(program));
        return program;
    }
}

// Marks one <img> in an ordinary page: replaces it with a canvas of the same
// layout that shows the marked image, and keeps the canvas at one device
// pixel per canvas pixel as the page reflows. Everything else on the page is
// untouched. Returns the ChromaWatermark so the page can change the payload,
// strength or enabled state later.
//
//     const mark = ChromaWatermark.attachToImage(img, { payload: 1234567 });
//
// Options: payload, strength, devicePixelRatio, and onRender(rect, width,
// height), called after every render with the canvas's viewport rectangle in
// CSS pixels and its size in device pixels.
ChromaWatermark.attachToImage = function (img, options = {}) {
    const dpr = options.devicePixelRatio || window.devicePixelRatio || 1;
    const canvas = document.createElement("canvas");
    canvas.className = img.className;
    if (img.id)
        canvas.id = img.id;
    canvas.style.cssText = img.style.cssText;
    canvas.setAttribute("role", "img");
    if (img.alt)
        canvas.setAttribute("aria-label", img.alt);

    const mark = new ChromaWatermark(canvas);
    mark.setPayload(options.payload ?? 0);
    mark.setStrength(options.strength ?? 0.08);

    const fit = () => {
        const rect = canvas.getBoundingClientRect();
        const width = Math.max(1, Math.round(rect.width * dpr));
        const height = Math.max(1, Math.round(rect.height * dpr));
        mark.resize(width, height);
        mark.render();
        if (options.onRender)
            options.onRender(rect, width, height);
    };

    const start = () => {
        mark.setSource(img);
        // Keep the image's shape unless the page's CSS sets an explicit
        // height; a canvas has no natural aspect of its own.
        if (!canvas.style.aspectRatio)
            canvas.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
        img.replaceWith(canvas);
        fit();
        // Size changes come through the observer; a position change alone
        // (a scrollbar appearing, fonts settling) does not, so re-measure on
        // load and on window resize too, for the sake of onRender's rect.
        new ResizeObserver(fit).observe(canvas);
        window.addEventListener("load", fit);
        window.addEventListener("resize", fit);
    };
    if (img.complete && img.naturalWidth > 0)
        start();
    else
        img.addEventListener("load", start, { once: true });

    mark.element = canvas;
    mark.refresh = fit;
    return mark;
};

// Exposed for tests: the pattern functions can be checked against the
// Python implementation without a GPU.
ChromaWatermark.hashU32 = hashU32;
ChromaWatermark.crc8 = crc8;
ChromaWatermark.describeCell = describeCell;
ChromaWatermark.cellPositionTable = cellPositionTable;
ChromaWatermark.amplitudeGrid = amplitudeGrid;
ChromaWatermark.LAYOUT = LAYOUT;

if (typeof module !== "undefined" && module.exports)
    module.exports = { ChromaWatermark, hashU32, crc8, describeCell, cellPositionTable, amplitudeGrid, LAYOUT };
