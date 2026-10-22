# Model attribution and license map

This map applies only to the weights named below. It does not apply a
repository license to unrelated files. The full license texts and notices are
installed under `/usr/share/comfygen-model-licenses/` in the baked image.

| Model files | License and attribution |
| --- | --- |
| `krea2_turbo_bf16.safetensors` | These Krea weights are provided under the Krea 2 Community License; downloading and using them requires agreement to that license. The complete agreement is included as `krea/LICENSE.pdf`; the required exact text notice is `krea/NOTICE`. Distribution requires providing the agreement and binding each recipient to its terms. The license also requires any distributed model name to begin with “Krea”. See [Krea 2 Community License](https://www.krea.ai/krea-2-licensing). |
| `qwen3vl_4b_fp8_scaled.safetensors`, `qwen3vl_4b_bf16.safetensors` | Apache-2.0 for the Qwen3-VL 4B encoder components; attribution: Qwen team, [Qwen/Qwen3-VL-4B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct). Full Apache-2.0 text is `apache/LICENSE`. |
| `wan2.1_14B_SCAIL_2_int8_convrot.safetensors`, `wan2.1_SCAIL_2_DPO_lora_bf16.safetensors` | MIT for these model weights as declared by the [zai-org/SCAIL-2 Hugging Face model](https://huggingface.co/zai-org/SCAIL-2). Authors: Wenhao Yan, Fengjia Guo, Zhuoyi Yang, and Jie Tang. Text is `scail/LICENSE`. The separate [official implementation repository](https://github.com/zai-org/SCAIL-2) is Apache-2.0; its code is not included in this image. |
| `Wan2_1_VAE_fp32.safetensors`, `wan_2.1_vae.safetensors`, `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `clip_vision_h.safetensors` | Apache-2.0 repackaged components from [Comfy-Org/Wan_2.1_ComfyUI_repackaged](https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged). Preserve Wan authorship and provenance; full license text is `apache/LICENSE`. |
| `seedvr2_ema_7b_fp16.safetensors`, `ema_vae_fp16.safetensors` | Apache-2.0 SeedVR2 weights; attribution to ByteDance-Seed, [SeedVR2-7B](https://huggingface.co/ByteDance-Seed/SeedVR2-7B). Full license text is `apache/LICENSE`. |
| `sam3.1_multiplex_fp16.safetensors` | SAM 3.1 custom license; the complete source license is included as `sam/LICENSE`. |
| `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` | Apache-2.0 LightX2V distillation weights; source [LightX2V model](https://huggingface.co/lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v). Kijai's WanVideo node documents the I2V extraction provenance. Full license text is `apache/LICENSE`. |
| `rife426.pth` | MIT RIFE weights; Copyright (c) 2021 hzwer. The Practical-RIFE license is included as `rife/LICENSE`. |
| `taew2_1.pth` | Apache-2.0 weights from [lightx2v/Autoencoders](https://huggingface.co/lightx2v/Autoencoders), full text `apache/LICENSE`. The separate taehv implementation code has MIT provenance, but that code is not included here. |

Three assets remain on the regional volume and are excluded from this image:
the QwenResearch noncommercial Qwen 3.5 9B encoder, the CC-BY-NC-SA-4 ITF
SkinDiffDetail model, and the Krea TextFusion LoRA whose author license is
unverified. Their pinned revisions, hashes, sizes, and reasons are recorded in
`regional-models.manifest.json`.
