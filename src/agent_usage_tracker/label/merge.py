"""Fold a LoRA into its base once, so mlx-lm can load it: run

    python -m agent_usage_tracker.label.merge evalengine-4b

with the extra aut[merge] (torch and peft, needed for this command only) and both repos in the
Hugging Face cache. It writes core.data_dir()/label/models/<name>; aut then needs only mlx-lm.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .backends import MODELS, cached_snapshot, merged_dir

BASES = {"evalengine-4b": ("Qwen/Qwen3.5-4B", "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a")}


def merge(name: str) -> Path:
    import torch
    from huggingface_hub import try_to_load_from_cache
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    m = MODELS[name]
    base_repo, base_rev = BASES[name]
    base = cached_snapshot(try_to_load_from_cache, base_repo, base_rev)
    adapter = try_to_load_from_cache(m.repo, "adapter_config.json", revision=m.revision)
    if base is None or not isinstance(adapter, str):
        raise SystemExit(f"label merge: fetch both first: hf download {base_repo} --revision "
                         f"{base_rev}, and hf download {m.repo} --revision {m.revision}")
    # AutoModelForCausalLM: another class silently dropped the adapter, hence the check.
    model = PeftModel.from_pretrained(
        AutoModelForCausalLM.from_pretrained(str(base), dtype=torch.bfloat16),
        str(Path(adapter).parent))
    if not any("lora_" in k for k, _ in model.named_parameters()):
        raise SystemExit(f"label merge: {m.repo} did not attach to {base_repo}; nothing written")
    out = merged_dir(name)
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    model.merge_and_unload().save_pretrained(tmp)
    AutoTokenizer.from_pretrained(str(base)).save_pretrained(tmp)
    cfg = json.loads((tmp / "config.json").read_text())
    (tmp / "config.json").write_text(json.dumps({**cfg, "model_type": "qwen3_5"}, indent=2))
    (tmp / "aut_merge.json").write_text(json.dumps(
        {"adapter": m.repo, "adapter_rev": m.revision, "base": base_repo, "base_rev": base_rev}))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)                            # a stopped merge leaves nothing that looks finished
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in BASES:
        raise SystemExit(f"usage: python -m agent_usage_tracker.label.merge {{{','.join(BASES)}}}")
    print(merge(sys.argv[1]))
