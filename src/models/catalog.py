"""SAGE Comprehensive Model Catalogue & Multi-Category Candidate Registry.

Defines the verified model ecosystem across 12 capability categories,
minimum 3 compatible candidates per skill/category, and explicit
architectural evaluation for LiquidAI/LFM3-Omni-2B.

Canonical Reference:
    PROMPT 081: Intelligence Architecture, Model Ecosystem & Voice
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModelCategory(str, Enum):
    GENERAL_BRAIN = "general_brain"
    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    AUDIO = "audio"
    MULTIMODAL = "multimodal"
    EMBEDDINGS = "embeddings"
    RERANKING = "reranking"
    TRANSLATION = "translation"
    DOCUMENTS = "documents"
    TOOL_CALLING = "tool_calling"
    SPECIALIZED = "specialized"


class ModelExecutionStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    NOT_INSTALLED = "NOT_INSTALLED"
    DOWNLOADING = "DOWNLOADING"
    INSTALLED = "INSTALLED"
    LOADING = "LOADING"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    FAILED = "FAILED"


# =============================================================================
# EXPLICIT ARCHITECTURAL EVALUATION: LiquidAI/LFM3-Omni-2B
# =============================================================================
LFM3_OMNI_EVALUATION: Dict[str, Any] = {
    "model_id": "LiquidAI/LFM3-Omni-2B",
    "name": "LFM3-Omni-2B (Liquid Foundation Model)",
    "repo_url": "https://huggingface.co/LiquidAI/LFM3-Omni-2B",
    "author": "Liquid AI",
    "parameter_count": "2.0B",
    "architecture": "Liquid Neural Network (Non-Transformer Hybrid Dynamical System)",
    "capabilities": {
        "text_input_output": True,
        "image_understanding": True,
        "audio_input": True,
        "speech_recognition": True,
        "speech_generation": False,  # Model emits tokens/text; lacks direct waveform synthesis head
        "video_capability": False,   # Official release processes interleaved image frames, not continuous video streams
        "tool_calling": True,
    },
    "formats": {
        "safetensors": True,
        "gguf_available": False,  # Proprietary liquid operators not merged into upstream llama.cpp GGUF specification
    },
    "compatible_runtimes": ["LiquidSDK", "PyTorch (liquid-foundation-models)", "ONNX (partial)"],
    "incompatible_runtimes": ["llama.cpp (standard)", "Ollama (standard)"],
    "hardware_requirements": {
        "vram_fp16_gb": 4.5,
        "vram_quant_gb": 2.2,
        "system_ram_gb": 8.0,
        "rtx3050_compatible": True,  # Fits within RTX 3050 6GB VRAM when running via PyTorch/CUDA
    },
    "runtime_verdict": (
        "LFM3-Omni-2B is a breakthrough 2B multimodal liquid model. However, standard llama.cpp and Ollama runtimes "
        "do not natively parse Liquid dynamical neural layers without LiquidSDK or custom C++ operators. "
        "Registered as SPECIALIZED / RESEARCH candidate; requires custom PyTorch provider adapter for execution."
    ),
}


# =============================================================================
# COMPREHENSIVE MODEL CATALOGUE (Minimum 3 Candidates per Category)
# =============================================================================
MODEL_CATALOGUE: Dict[str, List[Dict[str, Any]]] = {
    ModelCategory.GENERAL_BRAIN.value: [
        {
            "model_id": "qwen2.5-7b-instruct",
            "name": "Qwen 2.5 7B Instruct",
            "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
            "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
            "parameters": "7.6B",
            "size_gb": 4.68,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Primary general-purpose intelligence engine with exceptional multilingual, reasoning, and instruction following.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "gemma-2-9b-it",
            "name": "Gemma 2 9B Instruct",
            "repo_id": "google/gemma-2-9b-it-GGUF",
            "filename": "gemma-2-9b-it-q4_k_m.gguf",
            "parameters": "9.2B",
            "size_gb": 5.85,
            "quantization": "Q4_K_M",
            "context_window": 8192,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Google high-performance dialogue model with interleaved sliding window attention.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "phi-4-mini",
            "name": "Phi-4 Mini 3.8B Instruct",
            "repo_id": "microsoft/Phi-4-mini-instruct-GGUF",
            "filename": "phi-4-mini-instruct-q4_k_m.gguf",
            "parameters": "3.8B",
            "size_gb": 2.49,
            "quantization": "Q4_K_M",
            "context_window": 16384,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Microsoft compact powerhouse delivering high throughput and strong mathematical understanding.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.CODING.value: [
        {
            "model_id": "qwen2.5-coder-7b",
            "name": "Qwen 2.5 Coder 7B Instruct",
            "repo_id": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
            "filename": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
            "parameters": "7.6B",
            "size_gb": 4.68,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "State-of-the-art coding specialist with multi-file reasoning, refactoring, and test synthesis.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "deepseek-coder-v2-lite",
            "name": "DeepSeek Coder V2 Lite Instruct",
            "repo_id": "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct-GGUF",
            "filename": "deepseek-coder-v2-lite-instruct-q4_k_m.gguf",
            "parameters": "15.7B (2.4B active MoE)",
            "size_gb": 9.12,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Mixture-of-Experts coder with 338 programming languages support and syntax comprehension.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "devstral-small-24b",
            "name": "Mistral Devstral Small 24B",
            "repo_id": "mistralai/Devstral-Small-24B-GGUF",
            "filename": "devstral-small-24b-q4_k_m.gguf",
            "parameters": "24B",
            "size_gb": 14.2,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Mistral open code generation model optimized for agentic software workflows and repo navigation.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.REASONING.value: [
        {
            "model_id": "deepseek-r1-distill-qwen-1.5b",
            "name": "DeepSeek R1 Distill Qwen 1.5B",
            "repo_id": "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
            "filename": "deepseek-r1-distill-qwen-1.5b-q4_k_m.gguf",
            "parameters": "1.8B",
            "size_gb": 1.12,
            "quantization": "Q4_K_M",
            "context_window": 16384,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Ultra-lightweight reasoning engine producing verifiable chain-of-thought tokens with low latency.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "deepseek-r1-distill-qwen-7b",
            "name": "DeepSeek R1 Distill Qwen 7B",
            "repo_id": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B-GGUF",
            "filename": "deepseek-r1-distill-qwen-7b-q4_k_m.gguf",
            "parameters": "7.6B",
            "size_gb": 4.68,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Full mathematical and scientific reasoning model with rigorous self-verification traces.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "qwen2.5-14b-instruct",
            "name": "Qwen 2.5 14B Instruct",
            "repo_id": "Qwen/Qwen2.5-14B-Instruct-GGUF",
            "filename": "qwen2.5-14b-instruct-q4_k_m.gguf",
            "parameters": "14.7B",
            "size_gb": 9.25,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "High-capacity analytical engine for complex multi-step research synthesis and cross-domain reasoning.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.VISION.value: [
        {
            "model_id": "qwen2.5-vl-7b-instruct",
            "name": "Qwen 2.5 VL 7B Instruct",
            "repo_id": "Qwen/Qwen2.5-VL-7B-Instruct-GGUF",
            "filename": "qwen2.5-vl-7b-instruct-q4_k_m.gguf",
            "parameters": "7.6B",
            "size_gb": 4.82,
            "quantization": "Q4_K_M",
            "context_window": 16384,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Advanced vision-language model with native desktop UI grounding, chart parsing, and visual document OCR.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "gemma-3-4b-it",
            "name": "Gemma 3 4B Multimodal Instruct",
            "repo_id": "google/gemma-3-4b-it-GGUF",
            "filename": "gemma-3-4b-it-q4_k_m.gguf",
            "parameters": "4.2B",
            "size_gb": 2.85,
            "quantization": "Q4_K_M",
            "context_window": 8192,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Compact vision-text conversational model ideal for local screen inspection on 6GB VRAM.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "lfm3-omni-2b",
            "name": "LFM3-Omni-2B (Liquid AI)",
            "repo_id": "LiquidAI/LFM3-Omni-2B",
            "filename": "model.safetensors",
            "parameters": "2.0B",
            "size_gb": 3.95,
            "quantization": "FP16 / PyTorch",
            "context_window": 8192,
            "provider": "liquid-ai",
            "runtime": "pytorch",
            "description": "Non-transformer liquid dynamical multimodal model with audio, vision, and text understanding.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
    ],

    ModelCategory.AUDIO.value: [
        {
            "model_id": "whisper-small",
            "name": "OpenAI Whisper Small",
            "repo_id": "openai/whisper-small",
            "filename": "ggml-whisper-small.bin",
            "parameters": "244M",
            "size_gb": 0.48,
            "quantization": "Q5_0",
            "context_window": 448,
            "provider": "whisper.cpp",
            "runtime": "local-audio",
            "description": "Robust multilingual speech recognition engine supporting push-to-talk transcription.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "qwen3-asr",
            "name": "Qwen 3 Audio ASR",
            "repo_id": "Qwen/Qwen3-Audio-ASR",
            "filename": "qwen3-audio-asr.gguf",
            "parameters": "1.5B",
            "size_gb": 1.25,
            "quantization": "Q4_K_M",
            "context_window": 2048,
            "provider": "llama.cpp",
            "runtime": "local-audio",
            "description": "Zero-shot acoustic speech comprehension and vocal tone analysis.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "lfm3-omni-audio",
            "name": "LFM3-Omni-2B Audio Head",
            "repo_id": "LiquidAI/LFM3-Omni-2B",
            "filename": "model.safetensors",
            "parameters": "2.0B",
            "size_gb": 3.95,
            "quantization": "FP16",
            "context_window": 8192,
            "provider": "liquid-ai",
            "runtime": "pytorch",
            "description": "Liquid continuous acoustic understanding engine for conversational voice parsing.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
    ],

    ModelCategory.EMBEDDINGS.value: [
        {
            "model_id": "bge-small-en-v1.5",
            "name": "BAAI BGE Small EN v1.5",
            "repo_id": "BAAI/bge-small-en-v1.5",
            "filename": "bge-small-en-v1.5.gguf",
            "parameters": "33M",
            "size_gb": 0.07,
            "quantization": "FP16",
            "context_window": 512,
            "provider": "llama.cpp",
            "runtime": "local-embedding",
            "description": "High-density 384-dimensional dense semantic embedding engine for Unified Memory Engine.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "e5-base-v2",
            "name": "Microsoft E5 Base v2",
            "repo_id": "intfloat/e5-base-v2",
            "filename": "e5-base-v2.gguf",
            "parameters": "110M",
            "size_gb": 0.22,
            "quantization": "FP16",
            "context_window": 512,
            "provider": "llama.cpp",
            "runtime": "local-embedding",
            "description": "General text embeddings with passage/query prefixing for asymmetric semantic retrieval.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "gte-base-en-v1.5",
            "name": "Alibaba GTE Base EN v1.5",
            "repo_id": "Alibaba-NLP/gte-base-en-v1.5",
            "filename": "gte-base-en-v1.5.gguf",
            "parameters": "137M",
            "size_gb": 0.28,
            "quantization": "FP16",
            "context_window": 8192,
            "provider": "llama.cpp",
            "runtime": "local-embedding",
            "description": "Long-context embedding model supporting whole document chunking and vector indexing.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
    ],

    ModelCategory.TOOL_CALLING.value: [
        {
            "model_id": "qwen2.5-7b-instruct",
            "name": "Qwen 2.5 7B Instruct (Function Calling)",
            "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
            "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
            "parameters": "7.6B",
            "size_gb": 4.68,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Strict JSON schema adherence, multi-turn tool calling, and parameter validation.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "mistral-7b-instruct-v0.3",
            "name": "Mistral 7B Instruct v0.3",
            "repo_id": "mistralai/Mistral-7B-Instruct-v0.3-GGUF",
            "filename": "mistral-7b-instruct-v0.3-q4_k_m.gguf",
            "parameters": "7.3B",
            "size_gb": 4.37,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Official Mistral tool call grammar parser for desktop automation and CLI orchestration.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "hermes-3-llama-3.1-8b",
            "name": "Nous Hermes 3 Llama 3.1 8B",
            "repo_id": "NousResearch/Hermes-3-Llama-3.1-8B-GGUF",
            "filename": "hermes-3-llama-3.1-8b-q4_k_m.gguf",
            "parameters": "8.0B",
            "size_gb": 4.92,
            "quantization": "Q4_K_M",
            "context_window": 16384,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Uncensored agentic steering model specialized in structured JSON tool schemas and planning.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.MULTIMODAL.value: [
        {
            "model_id": "llava-1.6-mistral-7b",
            "name": "LLaVA 1.6 Mistral 7B",
            "repo_id": "cjpais/llava-1.6-mistral-7b-gguf",
            "filename": "llava-1.6-mistral-7b.Q4_K_M.gguf",
            "parameters": "7B",
            "size_gb": 4.37,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Vision-language model for image understanding, visual QA, and scene description.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "LFM3-Omni-2B",
            "name": "LFM3-Omni-2B (Liquid Foundation Model)",
            "repo_id": "LiquidAI/LFM3-Omni-2B",
            "filename": None,
            "parameters": "2B",
            "size_gb": 4.5,
            "quantization": None,
            "context_window": 32768,
            "provider": "LiquidSDK",
            "runtime": "pytorch-liquidsdk",
            "description": "Liquid AI 2B non-transformer multimodal model (text/image/audio). Requires LiquidSDK or PyTorch adapter.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "minicpm-v-2.6",
            "name": "MiniCPM-V 2.6",
            "repo_id": "openbmb/MiniCPM-V-2_6-gguf",
            "filename": "MiniCPM-V-2_6-Q4_K_M.gguf",
            "parameters": "8B",
            "size_gb": 5.1,
            "quantization": "Q4_K_M",
            "context_window": 32768,
            "provider": "llama.cpp",
            "runtime": "local-gguf",
            "description": "Compact yet capable multimodal VLM supporting image/video understanding and OCR.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.RERANKING.value: [
        {
            "model_id": "bge-reranker-v2-m3",
            "name": "BGE Reranker v2 M3",
            "repo_id": "BAAI/bge-reranker-v2-m3",
            "filename": None,
            "parameters": "568M",
            "size_gb": 0.57,
            "quantization": None,
            "context_window": 8192,
            "provider": "sentence-transformers",
            "runtime": "pytorch",
            "description": "State-of-the-art multilingual cross-encoder reranker for RAG pipelines.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "cross-encoder-ms-marco-minilm-l6",
            "name": "MS-MARCO MiniLM Cross-Encoder L6",
            "repo_id": "cross-encoder/ms-marco-MiniLM-L-6-v2",
            "filename": None,
            "parameters": "22M",
            "size_gb": 0.09,
            "quantization": None,
            "context_window": 512,
            "provider": "sentence-transformers",
            "runtime": "pytorch",
            "description": "Fast lightweight cross-encoder for passage reranking in retrieval-augmented generation.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "jina-reranker-v2-base-multilingual",
            "name": "Jina Reranker v2 Base Multilingual",
            "repo_id": "jinaai/jina-reranker-v2-base-multilingual",
            "filename": None,
            "parameters": "278M",
            "size_gb": 0.56,
            "quantization": None,
            "context_window": 8192,
            "provider": "sentence-transformers",
            "runtime": "pytorch",
            "description": "Multilingual reranker trained on diverse cross-lingual and code retrieval tasks.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.TRANSLATION.value: [
        {
            "model_id": "opus-mt-en-hi",
            "name": "OPUS-MT English to Hindi",
            "repo_id": "Helsinki-NLP/opus-mt-en-hi",
            "filename": None,
            "parameters": "74M",
            "size_gb": 0.3,
            "quantization": None,
            "context_window": 512,
            "provider": "transformers",
            "runtime": "pytorch",
            "description": "MarianMT model for high-quality English to Hindi translation.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "nllb-200-distilled-600m",
            "name": "NLLB-200 Distilled 600M",
            "repo_id": "facebook/nllb-200-distilled-600M",
            "filename": None,
            "parameters": "600M",
            "size_gb": 1.2,
            "quantization": None,
            "context_window": 1024,
            "provider": "transformers",
            "runtime": "pytorch",
            "description": "Meta's No Language Left Behind 200-language translation model — compact distilled variant.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "madlad400-3b-mt",
            "name": "MADLAD-400 3B Machine Translation",
            "repo_id": "google/madlad400-3b-mt",
            "filename": None,
            "parameters": "3B",
            "size_gb": 1.5,
            "quantization": None,
            "context_window": 1024,
            "provider": "transformers",
            "runtime": "pytorch",
            "description": "Google multilingual translation model supporting 400+ languages in a single model.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.DOCUMENTS.value: [
        {
            "model_id": "docling-ibm",
            "name": "Docling IBM Document Parser",
            "repo_id": "ds4sd/docling-models",
            "filename": None,
            "parameters": "80M",
            "size_gb": 0.33,
            "quantization": None,
            "context_window": 4096,
            "provider": "docling",
            "runtime": "pytorch",
            "description": "IBM Docling layout and table parser for PDFs, DOCX, and scanned documents.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "got-ocr2",
            "name": "GOT-OCR 2.0",
            "repo_id": "stepfun-ai/GOT-OCR2_0",
            "filename": None,
            "parameters": "580M",
            "size_gb": 1.2,
            "quantization": None,
            "context_window": 4096,
            "provider": "transformers",
            "runtime": "pytorch",
            "description": "General OCR Theory model for structured document and printed text recognition.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "layoutlmv3-base",
            "name": "LayoutLMv3 Base",
            "repo_id": "microsoft/layoutlmv3-base",
            "filename": None,
            "parameters": "125M",
            "size_gb": 0.5,
            "quantization": None,
            "context_window": 512,
            "provider": "transformers",
            "runtime": "pytorch",
            "description": "Microsoft document understanding model for form extraction, table parsing, and VQA.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],

    ModelCategory.SPECIALIZED.value: [
        {
            "model_id": "whisper-small-en",
            "name": "Whisper Small (English)",
            "repo_id": "openai/whisper-small.en",
            "filename": None,
            "parameters": "244M",
            "size_gb": 0.47,
            "quantization": None,
            "context_window": 448,
            "provider": "faster-whisper",
            "runtime": "pytorch",
            "description": "OpenAI Whisper English speech-to-text — compact, fast, and accurate for push-to-talk.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
        {
            "model_id": "LFM3-Omni-2B-research",
            "name": "LFM3-Omni-2B (Research / Liquid Neural Networks)",
            "repo_id": "LiquidAI/LFM3-Omni-2B",
            "filename": None,
            "parameters": "2B",
            "size_gb": 4.5,
            "quantization": None,
            "context_window": 32768,
            "provider": "LiquidSDK",
            "runtime": "pytorch-liquidsdk",
            "description": "Liquid Foundation Model — non-transformer architecture (dynamical state-space). Requires LiquidSDK.",
            "status": ModelExecutionStatus.DISCOVERED.value,
        },
        {
            "model_id": "sentence-transformers-paraphrase-minilm",
            "name": "Paraphrase-MiniLM-L6-v2",
            "repo_id": "sentence-transformers/paraphrase-MiniLM-L6-v2",
            "filename": None,
            "parameters": "22M",
            "size_gb": 0.09,
            "quantization": None,
            "context_window": 128,
            "provider": "sentence-transformers",
            "runtime": "pytorch",
            "description": "Ultra-fast sentence paraphrase / duplicate detection model for specialized classification tasks.",
            "status": ModelExecutionStatus.NOT_INSTALLED.value,
        },
    ],
}


def get_candidates_for_category(category: str) -> List[Dict[str, Any]]:
    """Return verified candidate models for a given capability category."""
    cat_key = category.lower()
    return MODEL_CATALOGUE.get(cat_key, MODEL_CATALOGUE[ModelCategory.GENERAL_BRAIN.value])


def get_model_from_catalogue(model_id: str) -> Optional[Dict[str, Any]]:
    """Lookup model specification across all categories."""
    clean_id = model_id.lower().replace("_", "-")
    for cat_list in MODEL_CATALOGUE.values():
        for m in cat_list:
            if m["model_id"].lower() == clean_id or m["name"].lower() == clean_id:
                return m
    return None


def evaluate_hardware_compatibility(
    model: Dict[str, Any],
    vram_available_gb: float = 4.0,
    ram_available_gb: float = 16.0,
) -> Dict[str, Any]:
    """Check hardware compatibility against local VRAM/RAM constraints (default RTX 3050 4GB)."""
    size_gb = float(model.get("size_gb") or ((model.get("size_mb") or 0) / 1024.0) or 0.0)
    provider = str(model.get("provider", model.get("provider_id", ""))).lower()

    if provider in ("gemini", "anthropic", "openai", "groq", "offline") or size_gb == 0:
        return {
            "compatible": True,
            "tier": "FULL_OFFLOAD",
            "message": "Zero VRAM/RAM required (Cloud / Resident). Fully compatible.",
            "recommended_gpu_layers": 0,
        }

    needed_vram = size_gb + 0.6
    if needed_vram <= vram_available_gb:
        return {
            "compatible": True,
            "tier": "FULL_GPU",
            "message": f"Full GPU offload supported (requires ~{needed_vram:.1f} GB / {vram_available_gb:.1f} GB VRAM).",
            "recommended_gpu_layers": 99,
        }
    elif size_gb <= ram_available_gb * 0.75:
        layers = int(max(0, (vram_available_gb - 0.5) / (needed_vram) * 33))
        return {
            "compatible": True,
            "tier": "PARTIAL_OFFLOAD",
            "message": f"Partial GPU offload / CPU inference supported (requires ~{size_gb:.1f} GB RAM, ~{layers} GPU layers).",
            "recommended_gpu_layers": layers,
        }
    else:
        return {
            "compatible": False,
            "tier": "INSUFFICIENT_MEMORY",
            "message": f"Model exceeds hardware capacity (requires {size_gb:.1f} GB, available {ram_available_gb:.1f} GB RAM).",
            "recommended_gpu_layers": 0,
        }

