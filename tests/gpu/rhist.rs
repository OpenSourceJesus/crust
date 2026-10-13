#[workgroup]
static mut BINS: [u32; 16] = [0; 16];

#[kernel(local_size = 32)]
fn hist(#[binding(0)] keys: &[u32], #[binding(1)] global: &mut [u32], n: u32) {
    let l: u32 = gpu_local_id(0);
    let g: u32 = gpu_global_id(0);
    unsafe {
        if l < 16 { gpu_atomic_store(&mut BINS[l as usize], 0); }
    }
    gpu_barrier();
    if g < n {
        let k: u32 = keys[g as usize];
        unsafe { gpu_atomic_add(&mut BINS[(k & 15) as usize], k.count_ones() & 1); }
    }
    gpu_barrier();
    unsafe {
        if l < 16 { gpu_atomic_add(&mut global[l as usize], gpu_atomic_load(&mut BINS[l as usize])); }
    }
}
