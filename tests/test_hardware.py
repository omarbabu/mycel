"""GPU detection from llama.cpp's own device list — no PyTorch."""
import os
import sys
from pathlib import Path

from mycel.hardware import bandwidth_for, check_serving_eligibility, parse_list_devices, probe, profile_from_devices

FAKE = [sys.executable, str(Path(__file__).parent / "fake_llama_server.py")]
METAL = """ggml_metal_init: loaded kernel
Available devices:
  BLAS: Accelerate (0 MiB, 0 MiB free)
  MTL0: Apple M3 Pro (28753 MiB, 28753 MiB free)
"""
CUDA2 = """Available devices:
  CUDA0: NVIDIA GeForce RTX 3090 (24135 MiB, 23700 MiB free)
  CUDA1: NVIDIA GeForce RTX 3090 (24135 MiB, 24000 MiB free)
  Vulkan0: NVIDIA GeForce RTX 3090 (24576 MiB, 24576 MiB free)
  Vulkan1: NVIDIA GeForce RTX 3090 (24576 MiB, 24576 MiB free)
"""
ROCM = "Available devices:\n  ROCm0: AMD Radeon RX 7900 XTX (24560 MiB, 24000 MiB free)\n"
VULKAN = "Available devices:\n  Vulkan0: AMD Radeon RX 6700 XT (RADV NAVI22) (12272 MiB, 11000 MiB free)\n"
CPU_ONLY = "Available devices:\n  CPU: Intel(R) Core(TM) i7 (0 MiB, 0 MiB free)\n"


def test_metal():
    hw = profile_from_devices(parse_list_devices(METAL), 36.0)
    assert (hw.accelerator, hw.device_name, hw.accel_memory_gb, hw.bandwidth_gbs) == ("mps", "Apple M3 Pro GPU", 28.1, 150.0)
    assert hw.tier == "T2" and hw.source == "llama.cpp" and not check_serving_eligibility(hw, 40)


def test_multi_gpu_cuda_is_summed_and_not_double_counted_with_vulkan():
    hw = profile_from_devices(parse_list_devices(CUDA2), 64.0)
    assert hw.accelerator == "cuda" and hw.device_name == "2x NVIDIA GeForce RTX 3090"
    assert hw.accel_memory_gb == 47.2 and len(hw.devices) == 2 and hw.bandwidth_gbs == 936.0


def test_rocm_vulkan_and_cpu_only():
    assert profile_from_devices(parse_list_devices(ROCM), 64).accelerator == "rocm"
    v = profile_from_devices(parse_list_devices(VULKAN), 32)
    assert v.accelerator == "vulkan" and v.accel_memory_gb == 12.0 and v.bandwidth_gbs == 384.0
    cpu = profile_from_devices(parse_list_devices(CPU_ONLY), 16, "Intel i7")
    assert cpu.accelerator == "cpu" and cpu.tier == "relay" and check_serving_eligibility(cpu, 40)


def test_bandwidth_table_prefers_specific_names():
    assert bandwidth_for("NVIDIA GeForce RTX 4070 Ti SUPER") == 672
    assert bandwidth_for("NVIDIA GeForce RTX 4070") == 504
    assert bandwidth_for("Apple M2 Max") == 400 and bandwidth_for("Apple M2") == 100
    assert bandwidth_for("Mystery Accelerator 9000") == 0.0


def test_probe_runs_the_engine_binary():
    os.environ["FAKE_LIST_DEVICES"] = ROCM
    try:
        hw = probe(FAKE)
    finally:
        del os.environ["FAKE_LIST_DEVICES"]
    assert hw.accelerator == "rocm" and hw.device_name == "AMD Radeon RX 7900 XTX" and hw.bandwidth_gbs == 960.0


def test_probe_without_any_engine_reports_cpu():
    hw = probe(["/nonexistent/llama-server"])
    assert hw.accelerator in ("cpu", "mps", "cuda")  # falls back (optionally to PyTorch), never crashes
