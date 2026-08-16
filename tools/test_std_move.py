#!/usr/bin/env python3
import os, random, time, subprocess, argparse
import matplotlib.pyplot as plt

# C++ Source Template
# Bypasses compile-time O3 dead-code elimination by accepting dynamic arguments
CPP_TEMPLATE = '''
#include <iostream>
#include <cstdlib>

class HeavyBuffer {
private:
    size_t size;
    unsigned char* data;

public:
    HeavyBuffer(size_t n, unsigned char init_val) : size(n), data(new unsigned char[n]) {
        for (size_t i = 0; i < size; ++i) {
            data[i] = init_val;
        }
    }

    ~HeavyBuffer() {
        delete[] data;
    }

    // Copy Constructor (O(N) deep copy)
    HeavyBuffer(const HeavyBuffer& other) : size(other.size), data(new unsigned char[other.size]) {
        for (size_t i = 0; i < size; ++i) {
            data[i] = other.data[i];
        }
    }

    // Move Constructor (O(1) pointer swap)
    HeavyBuffer(HeavyBuffer&& other) noexcept : size(other.size), data(other.data) {
        other.data = nullptr;
        other.size = 0;
    }

    // Prevents side-effects from being optimized out completely
    unsigned char get_sample() const {
        return data ? data[0] : 0;
    }
};

int main(int argc, char* argv[]) {
    if (argc < 3) return 1;

    size_t buffer_size = std::strtoull(argv[1], nullptr, 10);
    unsigned char init_val = static_cast<unsigned char>(std::atoi(argv[2]));

    HeavyBuffer source(buffer_size, init_val);

#ifdef USE_MOVE
    HeavyBuffer target = std::move(source);
#else
    HeavyBuffer target = source;
#endif

    // Prevent compiler from dropping unused variables
    std::cout << static_cast<int>(target.get_sample()) << std::endl;
    return 0;
}
'''


def compile_cpp(compiler, source_code, define_flag, output_bin):
    """Compiles the C++ template into an optimized executable."""
    cmd = [compiler, "-O3", "-std=c++11"]
    if define_flag:
        cmd.append(f"-D{define_flag}")
    cmd.extend(["-x", "c++", "-", "-o", output_bin])

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = process.communicate(input=source_code)

    if process.returncode != 0:
        raise RuntimeError(f"Compilation failed with {compiler}:\n{stderr}")


def benchmark_binary(bin_path, buffer_size, init_byte, runs=10):
    """Times execution using time.perf_counter across multiple runs."""
    durations = []
    cmd = [bin_path, str(buffer_size), str(init_byte)]

    for _ in range(runs):
        start = time.perf_counter()
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
        end = time.perf_counter()
        durations.append((end - start) * 1000.0)  # ms

    # Return average execution time
    return sum(durations) / len(durations)


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark C++ std::move vs. Deep Copy"
    )
    parser.add_argument(
        "--clang",
        action="store_true",
        help="Use clang++ instead of g++ for compilation",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=200_000_000,
        help="Buffer size in bytes (~200MB default)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=10,
        help="Number of iterations to average timing",
    )
    args = parser.parse_args()

    compiler = "clang++" if args.clang else "g++"
    init_byte = random.randint(1, 255)
    tmp_dir = '/tmp'

    copy_bin = os.path.join(tmp_dir, "test_copy")
    move_bin = os.path.join(tmp_dir, "test_move")

    print(
        f"Compiling with {compiler} -O3 (Buffer size: {args.size:,} bytes)..."
    )
    compile_cpp(compiler, CPP_TEMPLATE, None, copy_bin)
    compile_cpp(compiler, CPP_TEMPLATE, "USE_MOVE", move_bin)

    print(
        f"Running benchmarks ({args.runs} runs each, random byte: {init_byte})..."
    )
    copy_time = benchmark_binary(copy_bin, args.size, init_byte, args.runs)
    move_time = benchmark_binary(move_bin, args.size, init_byte, args.runs)

    print(f"\nResults ({compiler} -O3):")
    print(f"  Copy (without std::move): {copy_time:.3f} ms")
    print(f"  Move (with std::move):    {move_time:.3f} ms")
    print(f"  Speedup:                  {copy_time / move_time:.1f}x faster")

    # Plot results
    categories = ["Copy (without std::move)", "Move (with std::move)"]
    times = [copy_time, move_time]
    colors = ["#e74c3c", "#2ecc71"]

    plt.figure(figsize=(8, 5))
    bars = plt.bar(categories, times, color=colors, width=0.5)

    plt.ylabel("Execution Time (ms, lower is better)")
    plt.title(
        f"C++11 std::move vs. Deep Copy Performance ({compiler} -O3)\n"
        f"Buffer Size: {args.size / (1024 * 1024):.1f} MB"
    )
    plt.grid(axis="y", linestyle="--", alpha=0.7)

    for bar in bars:
        yval = bar.get_height()
        plt.text(
            bar.get_x() + bar.get_width() / 2.0,
            yval + (max(times) * 0.02),
            f"{yval:.2f} ms",
            ha="center",
            va="bottom",
            fontweight="bold",
        )

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()