// A Rust-subset kernel covering what the front end adds for the GPU:
// #[kernel], #[binding(N)], file-scope consts, bool helpers, f32 methods and
// an inferred gpu_global_id.
const SCALE: f32 = 2.5;
const LIMIT: u32 = 3;

fn inside(x: f32, lo: f32, hi: f32) -> bool {
    x >= lo && x <= hi
}

#[kernel(local_size = 8)]
fn shape(#[binding(2)] xs: &[f32], #[binding(5)] out: &mut [f32], bias: f32, n: u32) {
    let i = gpu_global_id(0);
    if i >= n {
        return;
    }
    let x: f32 = xs[i as usize];
    let mut acc: f32 = 0.0;
    for k in 0..LIMIT {
        acc += x.mul_add(SCALE, k as f32);
    }
    if inside(x, -1.0, 1.0) {
        acc = acc.max(bias);
    }
    out[i as usize] = acc.clamp(-10.0, 10.0).abs().sqrt() + x.fract();
}
