// Per-workgroup sum in Rust: workgroup memory, a barrier per step.
#[workgroup]
static mut SCRATCH: [u32; 64] = [0; 64];

#[kernel(local_size = 64)]
fn reduce(#[binding(0)] input: &[u32], #[binding(1)] sums: &mut [u32], n: u32) {
    let l: u32 = gpu_local_id(0);
    let g: u32 = gpu_global_id(0);
    let mut v: u32 = 0;
    if g < n {
        v = input[g as usize];
    }
    unsafe {
        SCRATCH[l as usize] = v;
    }
    gpu_barrier();
    let mut s: u32 = 32;
    while s > 0 {
        if l < s {
            unsafe {
                SCRATCH[l as usize] = SCRATCH[l as usize] + SCRATCH[(l + s) as usize];
            }
        }
        gpu_barrier();
        s = s >> 1;
    }
    if l == 0 {
        unsafe {
            sums[gpu_group_id(0) as usize] = SCRATCH[0];
        }
    }
}
