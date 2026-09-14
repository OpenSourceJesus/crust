// GLES2 + surfaceless-EGL import object for Crust wasm modules.
//
// Crust keeps sizeof(void*)==8 under --target wasm, so pointers stored in
// linear memory are 8 bytes; import *arguments* are narrowed to i32. This
// host reads both correctly.
//
// Two backends:
//   soft  -- enough of GLES2 to run examples/gles2/triangle.c under node
//            with no GPU and no npm deps (CI / make test_gles2_wasm)
//   webgl -- wrap a WebGLRenderingContext when one is available (browser)
//
//     const { createEnv } = require('./gles2_wasm_host.js');
//     const env = createEnv(() => memory);  // soft by default
//     const env = createEnv(() => memory, { gl });  // WebGL

'use strict';

const GL_DEPTH_BUFFER_BIT = 0x00000100;
const GL_STENCIL_BUFFER_BIT = 0x00000400;
const GL_COLOR_BUFFER_BIT = 0x00004000;
const GL_TRIANGLES = 0x0004;
const GL_ARRAY_BUFFER = 0x8892;
const GL_ELEMENT_ARRAY_BUFFER = 0x8893;
const GL_STATIC_DRAW = 0x88E4;
const GL_FRAMEBUFFER = 0x8D40;
const GL_RENDERBUFFER = 0x8D41;
const GL_RGBA4 = 0x8056;
const GL_COLOR_ATTACHMENT0 = 0x8CE0;
const GL_FRAMEBUFFER_COMPLETE = 0x8CD5;
const GL_COMPILE_STATUS = 0x8B81;
const GL_LINK_STATUS = 0x8B82;
const GL_FLOAT = 0x1406;
const GL_UNSIGNED_BYTE = 0x1401;
const GL_RGBA = 0x1908;
const GL_VERSION = 0x1F02;
const GL_FRAGMENT_SHADER = 0x8B30;
const GL_VERTEX_SHADER = 0x8B31;

function createSoftBackend(getMemory) {
  let nextId = 1;
  const shaders = new Map();
  const programs = new Map();
  const buffers = new Map();
  const framebuffers = new Map();
  const renderbuffers = new Map();

  let boundArrayBuffer = 0;
  let boundFramebuffer = 0;
  let boundRenderbuffer = 0;
  let currentProgram = 0;
  let viewport = { x: 0, y: 0, w: 1, h: 1 };
  let clearColor = [0, 0, 0, 1];
  const attribEnable = [false, false, false, false];
  const attribPtr = [null, null, null, null];

  // Scratch strings returned by glGetString live in wasm memory.
  let stringScratch = 0;

  function mem() {
    return getMemory();
  }
  function u8() {
    return new Uint8Array(mem().buffer);
  }
  function dv() {
    return new DataView(mem().buffer);
  }
  function readCString(ptr) {
    if (!ptr) return '';
    const bytes = u8();
    let end = ptr >>> 0;
    while (bytes[end]) end++;
    return Buffer.from(bytes.subarray(ptr >>> 0, end)).toString('utf8');
  }
  function readPtr64(addr) {
    // In-memory pointers are 8 bytes (Crust dual ABI).
    return Number(dv().getBigUint64(addr >>> 0, true) & 0xffffffffn);
  }
  function writeCString(s) {
    const bytes = u8();
    if (!stringScratch) {
      // Park returned strings near the end of the current memory image.
      stringScratch = Math.max(256, bytes.length - 4096);
    }
    const start = stringScratch;
    for (let i = 0; i < s.length; i++) bytes[start + i] = s.charCodeAt(i) & 0xff;
    bytes[start + s.length] = 0;
    stringScratch = start + s.length + 1;
    return start;
  }
  function colorTarget() {
    if (!boundFramebuffer) return null;
    const fb = framebuffers.get(boundFramebuffer);
    if (!fb || !fb.colorRb) return null;
    return renderbuffers.get(fb.colorRb);
  }

  function drawTriangles(first, count) {
    const rb = colorTarget();
    if (!rb || !rb.pixels) return;
    const prog = programs.get(currentProgram);
    if (!prog) return;
    const posA = attribPtr[0];
    const colA = attribPtr[1];
    if (!posA || !colA || !attribEnable[0] || !attribEnable[1]) return;
    const buf = buffers.get(boundArrayBuffer);
    if (!buf || !buf.data) return;

    const verts = [];
    for (let i = 0; i < count; i++) {
      const vi = first + i;
      const po = posA.offset + vi * posA.stride;
      const co = colA.offset + vi * colA.stride;
      const x = buf.data.getFloat32(po, true);
      const y = buf.data.getFloat32(po + 4, true);
      const r = buf.data.getFloat32(co, true);
      const g = buf.data.getFloat32(co + 4, true);
      const b = buf.data.getFloat32(co + 8, true);
      // NDC -> window (GL bottom-left origin).
      const sx = viewport.x + (x + 1) * 0.5 * viewport.w;
      const sy = viewport.y + (y + 1) * 0.5 * viewport.h;
      verts.push({ x: sx, y: sy, r, g, b });
    }

    for (let i = 0; i + 2 < verts.length; i += 3) {
      fillTri(rb, verts[i], verts[i + 1], verts[i + 2]);
    }
  }

  function fillTri(rb, a, b, c) {
    const minX = Math.max(0, Math.floor(Math.min(a.x, b.x, c.x)));
    const maxX = Math.min(rb.w - 1, Math.ceil(Math.max(a.x, b.x, c.x)));
    const minY = Math.max(0, Math.floor(Math.min(a.y, b.y, c.y)));
    const maxY = Math.min(rb.h - 1, Math.ceil(Math.max(a.y, b.y, c.y)));
    const area = edge(a, b, c);
    if (Math.abs(area) < 1e-8) return;
    for (let y = minY; y <= maxY; y++) {
      for (let x = minX; x <= maxX; x++) {
        const p = { x: x + 0.5, y: y + 0.5 };
        const w0 = edge(b, c, p);
        const w1 = edge(c, a, p);
        const w2 = edge(a, b, p);
        if (area > 0) {
          if (w0 < 0 || w1 < 0 || w2 < 0) continue;
        } else if (w0 > 0 || w1 > 0 || w2 > 0) {
          continue;
        }
        const iw0 = w0 / area;
        const iw1 = w1 / area;
        const iw2 = w2 / area;
        const r = Math.min(255, Math.max(0, Math.round((iw0 * a.r + iw1 * b.r + iw2 * c.r) * 255)));
        const g = Math.min(255, Math.max(0, Math.round((iw0 * a.g + iw1 * b.g + iw2 * c.g) * 255)));
        const bl = Math.min(255, Math.max(0, Math.round((iw0 * a.b + iw1 * b.b + iw2 * c.b) * 255)));
        const o = (y * rb.w + x) * 4;
        rb.pixels[o] = r;
        rb.pixels[o + 1] = g;
        rb.pixels[o + 2] = bl;
        rb.pixels[o + 3] = 255;
      }
    }
  }

  function edge(a, b, c) {
    return (c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x);
  }

  return {
    // ---- EGL (surfaceless stubs; real work is the FBO path) ----
    eglGetDisplay(_native) { return 1; },
    eglInitialize(_dpy, majorPtr, minorPtr) {
      const d = dv();
      if (majorPtr) d.setInt32(majorPtr >>> 0, 1, true);
      if (minorPtr) d.setInt32(minorPtr >>> 0, 5, true);
      return 1;
    },
    eglGetError() { return 0x3000; }, // EGL_SUCCESS
    eglBindAPI(_api) { return 1; },
    eglChooseConfig(_dpy, _attribs, cfgPtr, cfgSize, numPtr) {
      const d = dv();
      if (cfgSize > 0 && cfgPtr) d.setBigUint64(cfgPtr >>> 0, 1n, true);
      if (numPtr) d.setInt32(numPtr >>> 0, 1, true);
      return 1;
    },
    eglCreateContext(_dpy, _cfg, _share, _attribs) { return 2; },
    eglMakeCurrent(_dpy, _draw, _read, _ctx) { return 1; },

    // ---- GLES2 ----
    glCreateShader(type) {
      const id = nextId++;
      shaders.set(id, { type, source: '', compiled: false });
      return id;
    },
    glShaderSource(shader, count, stringsPtr, lengthPtr) {
      const sh = shaders.get(shader);
      if (!sh) return;
      let src = '';
      for (let i = 0; i < count; i++) {
        const sp = readPtr64((stringsPtr >>> 0) + i * 8);
        if (lengthPtr) {
          const len = dv().getInt32((lengthPtr >>> 0) + i * 4, true);
          if (len >= 0) {
            src += Buffer.from(u8().subarray(sp, sp + len)).toString('utf8');
            continue;
          }
        }
        src += readCString(sp);
      }
      sh.source = src;
    },
    glCompileShader(shader) {
      const sh = shaders.get(shader);
      if (sh) sh.compiled = true;
    },
    glGetShaderiv(shader, pname, params) {
      const sh = shaders.get(shader);
      if (!params) return;
      if (pname === GL_COMPILE_STATUS) {
        dv().setInt32(params >>> 0, sh && sh.compiled ? 1 : 0, true);
      }
    },
    glGetShaderInfoLog(_shader, maxLen, lengthPtr, infoLog) {
      if (lengthPtr) dv().setInt32(lengthPtr >>> 0, 0, true);
      if (infoLog && maxLen > 0) u8()[infoLog >>> 0] = 0;
    },
    glCreateProgram() {
      const id = nextId++;
      programs.set(id, { shaders: [], linked: false, attribs: {} });
      return id;
    },
    glAttachShader(program, shader) {
      const p = programs.get(program);
      if (p) p.shaders.push(shader);
    },
    glBindAttribLocation(program, index, namePtr) {
      const p = programs.get(program);
      if (p) p.attribs[readCString(namePtr)] = index;
    },
    glLinkProgram(program) {
      const p = programs.get(program);
      if (p) p.linked = true;
    },
    glGetProgramiv(program, pname, params) {
      const p = programs.get(program);
      if (!params) return;
      if (pname === GL_LINK_STATUS) {
        dv().setInt32(params >>> 0, p && p.linked ? 1 : 0, true);
      }
    },
    glGetProgramInfoLog(_program, maxLen, lengthPtr, infoLog) {
      if (lengthPtr) dv().setInt32(lengthPtr >>> 0, 0, true);
      if (infoLog && maxLen > 0) u8()[infoLog >>> 0] = 0;
    },
    glDeleteShader(_shader) {},
    glGetString(name) {
      if (name === GL_VERSION) return writeCString('OpenGL ES 2.0 (Crust soft)');
      return writeCString('');
    },
    glGenFramebuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const id = nextId++;
        framebuffers.set(id, { colorRb: 0 });
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindFramebuffer(target, fb) {
      if (target === GL_FRAMEBUFFER) boundFramebuffer = fb;
    },
    glGenRenderbuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const id = nextId++;
        renderbuffers.set(id, { w: 0, h: 0, pixels: null });
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindRenderbuffer(target, rb) {
      if (target === GL_RENDERBUFFER) boundRenderbuffer = rb;
    },
    glRenderbufferStorage(target, internalformat, width, height) {
      if (target !== GL_RENDERBUFFER) return;
      const rb = renderbuffers.get(boundRenderbuffer);
      if (!rb) return;
      rb.w = width;
      rb.h = height;
      rb.format = internalformat;
      rb.pixels = new Uint8Array(width * height * 4);
    },
    glFramebufferRenderbuffer(target, attachment, rbtarget, rb) {
      if (target !== GL_FRAMEBUFFER) return;
      const fb = framebuffers.get(boundFramebuffer);
      if (!fb) return;
      if (attachment === GL_COLOR_ATTACHMENT0 && rbtarget === GL_RENDERBUFFER) {
        fb.colorRb = rb;
      }
    },
    glCheckFramebufferStatus(target) {
      if (target !== GL_FRAMEBUFFER) return 0;
      const fb = framebuffers.get(boundFramebuffer);
      if (!fb || !fb.colorRb) return 0;
      const rb = renderbuffers.get(fb.colorRb);
      return rb && rb.pixels ? GL_FRAMEBUFFER_COMPLETE : 0;
    },
    glGenBuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const id = nextId++;
        buffers.set(id, { data: null });
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindBuffer(target, buf) {
      if (target === GL_ARRAY_BUFFER) boundArrayBuffer = buf;
    },
    glBufferData(target, size, dataPtr, _usage) {
      if (target !== GL_ARRAY_BUFFER) return;
      const buf = buffers.get(boundArrayBuffer);
      if (!buf) return;
      const n = typeof size === 'bigint' ? Number(size) : Number(size);
      const src = u8().subarray(dataPtr >>> 0, (dataPtr >>> 0) + n);
      const copy = new Uint8Array(n);
      copy.set(src);
      buf.data = new DataView(copy.buffer);
    },
    glViewport(x, y, w, h) {
      viewport = { x, y, w, h };
    },
    glClearColor(r, g, b, a) {
      clearColor = [r, g, b, a];
    },
    glClear(mask) {
      const rb = colorTarget();
      if (!rb || !rb.pixels) return;
      if (mask & GL_COLOR_BUFFER_BIT) {
        const r = Math.round(clearColor[0] * 255);
        const g = Math.round(clearColor[1] * 255);
        const b = Math.round(clearColor[2] * 255);
        const a = Math.round(clearColor[3] * 255);
        for (let i = 0; i < rb.pixels.length; i += 4) {
          rb.pixels[i] = r;
          rb.pixels[i + 1] = g;
          rb.pixels[i + 2] = b;
          rb.pixels[i + 3] = a;
        }
      }
    },
    glUseProgram(program) {
      currentProgram = program;
    },
    glEnableVertexAttribArray(index) {
      attribEnable[index] = true;
    },
    glVertexAttribPointer(index, size, type, normalized, stride, offset) {
      const off = typeof offset === 'bigint' ? Number(offset) : Number(offset);
      attribPtr[index] = {
        size, type, normalized, stride: stride || size * 4, offset: off,
      };
    },
    glDrawArrays(mode, first, count) {
      if (mode === GL_TRIANGLES) drawTriangles(first, count);
    },
    glFinish() {},
    glReadPixels(x, y, width, height, format, type, pixelsPtr) {
      const rb = colorTarget();
      if (!rb || !rb.pixels) return;
      if (format !== GL_RGBA || type !== GL_UNSIGNED_BYTE) return;
      const dst = u8();
      let di = pixelsPtr >>> 0;
      for (let row = 0; row < height; row++) {
        const sy = y + row;
        for (let col = 0; col < width; col++) {
          const sx = x + col;
          const si = (sy * rb.w + sx) * 4;
          dst[di++] = rb.pixels[si];
          dst[di++] = rb.pixels[si + 1];
          dst[di++] = rb.pixels[si + 2];
          dst[di++] = rb.pixels[si + 3];
        }
      }
    },
  };
}

function createWebGLBackend(getMemory, gl) {
  // Map the GLES2 entry points the triangle uses onto WebGL. EGL stays stubbed.
  const soft = createSoftBackend(getMemory); // reuse EGL stubs + helpers
  const shaders = new Map();
  const programs = new Map();
  const buffers = new Map();
  const framebuffers = new Map();
  const renderbuffers = new Map();
  let nextId = 1;

  function mem() { return getMemory(); }
  function u8() { return new Uint8Array(mem().buffer); }
  function dv() { return new DataView(mem().buffer); }
  function readCString(ptr) {
    if (!ptr) return '';
    const bytes = u8();
    let end = ptr >>> 0;
    while (bytes[end]) end++;
    return new TextDecoder().decode(bytes.subarray(ptr >>> 0, end));
  }
  function readPtr64(addr) {
    return Number(dv().getBigUint64(addr >>> 0, true) & 0xffffffffn);
  }

  const env = {
    eglGetDisplay: soft.eglGetDisplay,
    eglInitialize: soft.eglInitialize,
    eglGetError: soft.eglGetError,
    eglBindAPI: soft.eglBindAPI,
    eglChooseConfig: soft.eglChooseConfig,
    eglCreateContext: soft.eglCreateContext,
    eglMakeCurrent: soft.eglMakeCurrent,

    glCreateShader(type) {
      const s = gl.createShader(type);
      const id = nextId++;
      shaders.set(id, s);
      return id;
    },
    glShaderSource(shader, count, stringsPtr, lengthPtr) {
      let src = '';
      for (let i = 0; i < count; i++) {
        const sp = readPtr64((stringsPtr >>> 0) + i * 8);
        src += readCString(sp);
      }
      gl.shaderSource(shaders.get(shader), src);
    },
    glCompileShader(shader) { gl.compileShader(shaders.get(shader)); },
    glGetShaderiv(shader, pname, params) {
      const v = gl.getShaderParameter(shaders.get(shader), pname);
      dv().setInt32(params >>> 0, v ? 1 : 0, true);
    },
    glGetShaderInfoLog(shader, maxLen, lengthPtr, infoLog) {
      const log = gl.getShaderInfoLog(shaders.get(shader)) || '';
      const n = Math.min(maxLen - 1, log.length);
      const bytes = u8();
      for (let i = 0; i < n; i++) bytes[(infoLog >>> 0) + i] = log.charCodeAt(i);
      bytes[(infoLog >>> 0) + n] = 0;
      if (lengthPtr) dv().setInt32(lengthPtr >>> 0, n, true);
    },
    glCreateProgram() {
      const p = gl.createProgram();
      const id = nextId++;
      programs.set(id, p);
      return id;
    },
    glAttachShader(program, shader) {
      gl.attachShader(programs.get(program), shaders.get(shader));
    },
    glBindAttribLocation(program, index, namePtr) {
      gl.bindAttribLocation(programs.get(program), index, readCString(namePtr));
    },
    glLinkProgram(program) { gl.linkProgram(programs.get(program)); },
    glGetProgramiv(program, pname, params) {
      const v = gl.getProgramParameter(programs.get(program), pname);
      dv().setInt32(params >>> 0, v ? 1 : 0, true);
    },
    glGetProgramInfoLog(program, maxLen, lengthPtr, infoLog) {
      const log = gl.getProgramInfoLog(programs.get(program)) || '';
      const n = Math.min(maxLen - 1, log.length);
      const bytes = u8();
      for (let i = 0; i < n; i++) bytes[(infoLog >>> 0) + i] = log.charCodeAt(i);
      bytes[(infoLog >>> 0) + n] = 0;
      if (lengthPtr) dv().setInt32(lengthPtr >>> 0, n, true);
    },
    glDeleteShader(shader) { gl.deleteShader(shaders.get(shader)); },
    glGetString(name) {
      // Reuse soft's linear-memory string writer via a tiny local copy.
      return soft.glGetString(name === GL_VERSION ? GL_VERSION : name);
    },
    glGenFramebuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const fb = gl.createFramebuffer();
        const id = nextId++;
        framebuffers.set(id, fb);
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindFramebuffer(target, fb) {
      gl.bindFramebuffer(target, fb ? framebuffers.get(fb) : null);
    },
    glGenRenderbuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const rb = gl.createRenderbuffer();
        const id = nextId++;
        renderbuffers.set(id, rb);
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindRenderbuffer(target, rb) {
      gl.bindRenderbuffer(target, rb ? renderbuffers.get(rb) : null);
    },
    glRenderbufferStorage(target, internalformat, width, height) {
      // WebGL1 has RGBA4; fall back to RGBA4 enum value.
      gl.renderbufferStorage(target, gl.RGBA4 || internalformat, width, height);
    },
    glFramebufferRenderbuffer(target, attachment, rbtarget, rb) {
      gl.framebufferRenderbuffer(target, attachment, rbtarget,
                                 rb ? renderbuffers.get(rb) : null);
    },
    glCheckFramebufferStatus(target) {
      return gl.checkFramebufferStatus(target);
    },
    glGenBuffers(n, ids) {
      for (let i = 0; i < n; i++) {
        const buf = gl.createBuffer();
        const id = nextId++;
        buffers.set(id, buf);
        dv().setUint32((ids >>> 0) + i * 4, id, true);
      }
    },
    glBindBuffer(target, buf) {
      gl.bindBuffer(target, buf ? buffers.get(buf) : null);
    },
    glBufferData(target, size, dataPtr, usage) {
      const n = typeof size === 'bigint' ? Number(size) : Number(size);
      const src = u8().subarray(dataPtr >>> 0, (dataPtr >>> 0) + n);
      gl.bufferData(target, src, usage || gl.STATIC_DRAW);
    },
    glViewport(x, y, w, h) { gl.viewport(x, y, w, h); },
    glClearColor(r, g, b, a) { gl.clearColor(r, g, b, a); },
    glClear(mask) { gl.clear(mask); },
    glUseProgram(program) { gl.useProgram(program ? programs.get(program) : null); },
    glEnableVertexAttribArray(index) { gl.enableVertexAttribArray(index); },
    glVertexAttribPointer(index, size, type, normalized, stride, offset) {
      const off = typeof offset === 'bigint' ? Number(offset) : Number(offset);
      gl.vertexAttribPointer(index, size, type, !!normalized, stride, off);
    },
    glDrawArrays(mode, first, count) { gl.drawArrays(mode, first, count); },
    glFinish() { gl.finish(); },
    glReadPixels(x, y, width, height, format, type, pixelsPtr) {
      const out = new Uint8Array(mem().buffer, pixelsPtr >>> 0, width * height * 4);
      gl.readPixels(x, y, width, height, format, type, out);
    },
  };
  return env;
}

/**
 * @param {() => WebAssembly.Memory} getMemory
 * @param {{ gl?: WebGLRenderingContext }} [opts]
 */
function createEnv(getMemory, opts) {
  opts = opts || {};
  if (opts.gl) return createWebGLBackend(getMemory, opts.gl);
  return createSoftBackend(getMemory);
}

module.exports = { createEnv, createSoftBackend, createWebGLBackend };
// Browser pages can load this file as a classic script.
if (typeof globalThis !== 'undefined') {
  globalThis.CrustGLES = { createEnv, createSoftBackend, createWebGLBackend };
}
