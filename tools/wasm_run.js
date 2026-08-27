// Minimal WASI preview-1 host, enough to run what the wasm back end emits.
//
//     node tools/wasm_run.js prog.wasm
//
// Implemented deliberately rather than reaching for node's built-in `node:wasi`:
// this is a few dozen lines, has no experimental-flag warnings to filter out of
// the test output, and -- most usefully -- it is explicit about exactly which
// host calls the compiler depends on. tools/wasm_difftest.py also cross-checks
// one module against node's real WASI, so conformance is not just taken on
// this file's word.
//
// Exits with the status the module passed to proc_exit, so stdout and the exit
// code can both be compared against a natively-compiled build.

const fs = require('fs');

const path = process.argv[2];
if (!path) {
  console.error('usage: wasm_run.js <module.wasm>');
  process.exit(2);
}

let memory = null;
let exited = false;
let exitCode = 0;

// Thrown by proc_exit to unwind out of _start. WASI's proc_exit does not
// return, and there is no other way to stop a running module from the host.
class ExitSignal extends Error {}

function mem8() { return new Uint8Array(memory.buffer); }
function view() { return new DataView(memory.buffer); }

const ERRNO_SUCCESS = 0;
const ERRNO_BADF = 8;

function fd_write(fd, iovsPtr, iovsLen, nwrittenPtr) {
  if (fd !== 1 && fd !== 2) return ERRNO_BADF;
  const dv = view();
  const bytes = mem8();
  let total = 0;
  const chunks = [];
  for (let i = 0; i < iovsLen; i++) {
    // Each iovec is two little-endian 32-bit fields: pointer, then length.
    const base = iovsPtr + i * 8;
    const ptr = dv.getUint32(base, true);
    const len = dv.getUint32(base + 4, true);
    chunks.push(bytes.subarray(ptr, ptr + len));
    total += len;
  }
  const buf = Buffer.concat(chunks.map(Buffer.from));
  // Write synchronously so output ordering survives proc_exit unwinding.
  fs.writeSync(fd, buf);
  dv.setUint32(nwrittenPtr, total, true);
  return ERRNO_SUCCESS;
}

function proc_exit(code) {
  exited = true;
  exitCode = code;
  throw new ExitSignal();
}

// Unimplemented calls return ENOSYS rather than trapping, so a program that
// probes for a capability degrades instead of dying.
const ENOSYS = 52;
const stub = () => ENOSYS;

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
    const module = await WebAssembly.compile(bytes);   // validates
    instance = await WebAssembly.instantiate(module, {
      wasi_snapshot_preview1: wasi,
      // A plain JS host module, for programs that declare an undefined
      // function without meaning a WASI call.
      env: new Proxy({}, {
        get: (_, name) => () => {
          throw new Error('unimplemented env import: ' + String(name));
        },
        has: () => true,
      }),
    });
  } catch (e) {
    console.error('' + e);
    process.exit(1);
  }

  memory = instance.exports.memory;

  try {
    if (typeof instance.exports._start === 'function') {
      instance.exports._start();
    } else if (typeof instance.exports.main === 'function') {
      // No entry point was synthesised (no main): fall back to calling main
      // directly and treating its return value as the status.
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

  // Report mode: the harness needs the status on a channel that cannot be
  // confused with a host error, because every one of the 256 exit statuses is
  // a legal answer. The marker goes to stderr so it never pollutes the stdout
  // being compared, and the process exits 0 so a nonzero exit unambiguously
  // means "the host failed", not "the program returned that".
  if (process.env.WASM_RUN_REPORT) {
    process.stderr.write('RESULT ' + (exitCode & 0xFF) + '\n');
    process.exit(0);
  }
  process.exit(exitCode & 0xFF);
})();
