from __future__ import annotations


def transformer_load_kwargs(torch_module) -> dict:
    """Load kwargs for the Gate 1-verified CUDA/fp16 stack.

    RTX 2080 Ti is Turing: the model configs advertise bfloat16, but only fp16
    works. CUDA loads are pinned to ``cuda:0`` with SDPA attention; CPU is a
    reasonable float32 fallback without a device map.
    """
    if torch_module.cuda.is_available():
        return {
            "dtype": torch_module.float16,
            "device_map": "cuda:0",
            "attn_implementation": "sdpa",
        }
    return {
        "dtype": torch_module.float32,
        "attn_implementation": "sdpa",
    }
