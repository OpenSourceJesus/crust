#!/usr/bin/env node
// Run a Crust --target wasm GLES2 module under node.
//
//     node tools/gles2_wasm_run.js build/gles2_triangle.wasm
//
// Supplies wasi_snapshot_preview1 (stdout via fd_write) and an `env` object
// that implements the EGL/GLES2 imports examples/gles2/triangle.c needs.
// Uses the soft backend in gles2_wasm_host.js -- no GPU, no npm packages.

'use strict';

const fs = require('fs');
const { createEnv } = require('./gles2_wasm_host.js');

const path = process.argv[2];
if (!path) {
  console.error('usage: gles2_wasm_run.js <module.wasm>');
  process.exit(2);
}

let memory = null;
let exited = false;
let exitCode = 0;

class ExitSignal extends Error {}

function mem8() { return new Uint8Array(memory.buffer); }
function view() { return new DataView(memory.buffer); }

const ERRNO_SUCCESS = 0;
const ERRNO_BADF = 8;
const ENOSYS = 52;
const stub = () => ENOSYS;

function fd_write(fd, iovsPtr, iovsLen, nwrittenPtr) {
  if (fd !== 1 && fd !== 2) return ERRNO_BADF;
  const dv = view();
  const bytes = mem8();
  let total = 0;
  const chunks = [];
  for (let i = 0; i < iovsLen; i++) {
    const base = iovsPtr + i * 8;
    const ptr = dv.getUint32(base, true);
    const len = dv.getUint32(base + 4, true);
    chunks.push(bytes.subarray(ptr, ptr + len));
    total += len;
  }
  const buf = Buffer.concat(chunks.map(Buffer.from));
  fs.writeSync(fd, buf);
  dv.setUint32(nwrittenPtr, total, true);
  return ERRNO_SUCCESS;
}

function proc_exit(code) {
  exited = true;
  exitCode = code;
  throw new ExitSignal();
}

const wasi = {
  fd_write,
  proc_exit,
  fd_read: stub,
  fd_close: stub,
  fd_seek: stub,
  fd_fdstat_get: stub,
  path_open: stub,
  environ_get: stub,
  environ_sizes_get: (countPtr, sizePtr) => {
    const dv = view();
    dv.setUint32(countPtr, 0, true);
    dv.setUint32(sizePtr, 0, true);
    return ERRNO_SUCCESS;
  },
  args_get: stub,
  args_sizes_get: (argcPtr, sizePtr) => {
    const dv = view();
    dv.setUint32(argcPtr, 0, true);
    dv.setUint32(sizePtr, 0, true);
    return ERRNO_SUCCESS;
  },
  random_get: stub,
  clock_time_get: stub,
};

(async () => {
  let instance;
  try {
    const bytes = fs.readFileSync(path);
    const env = createEnv(() => memory);
    const result = await WebAssembly.instantiate(bytes, {
      wasi_snapshot_preview1: wasi,
      env,
    });
    instance = result.instance;
  } catch (e) {
    console.error('' + e);
    process.exit(1);
  }

  memory = instance.exports.memory;

  try {
    if (typeof instance.exports._start === 'function') {
      instance.exports._start();
    } else if (typeof instance.exports.main === 'function') {
      const r = Number(instance.exports.main());
      exitCode = ((r % 256) + 256) % 256;
    } else {
      console.error('module exports neither _start nor main');
      process.exit(1);
    }
  } catch (e) {
    if (!(e instanceof ExitSignal)) {
      console.error('' + e);
      process.exit(1);
    }
  }

  if (process.env.WASM_RUN_REPORT) {
    process.stderr.write('RESULT ' + (exitCode & 0xFF) + '\n');
    process.exit(0);
  }
  process.exit(exitCode & 0xFF);
})();
