"""
BGE-M3 中文向量模型封装（singleton）。

- 模型：BAAI/bge-m3 —— 中文效果好、1024 维、CPU 可跑、零调用成本
- 首次加载会从 HuggingFace 下载 ~2.3GB；缓存位置遵循
  SENTENCE_TRANSFORMERS_HOME / HF_HOME（Windows 安装器默认放在 D 盘运行时目录）
- 之后每次启动 ~10–20 秒装载到内存
- 单例：第一次调用时加载，后续直接复用
"""
from __future__ import annotations

import threading
from typing import List, Optional

import numpy as np
from loguru import logger


# 模型名（如果哪天想换，改这一行）
MODEL_NAME = "BAAI/bge-m3"

# 维度（必须和 db.py 里 VEC_SCHEMA_SQL 的 FLOAT[1024] 对齐）
EMBED_DIM = 1024


class _EncoderSingleton:
    """单例 wrapper。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._model = None
        return cls._instance

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        # 延迟导入，避免没用搜索功能时也得装 torch
        from sentence_transformers import SentenceTransformer

        logger.info(
            "Loading embedding model {} (首次会下载 ~2.3GB，后续就快了)...",
            MODEL_NAME,
        )
        self._model = SentenceTransformer(MODEL_NAME, device="cpu")
        # 校验维度
        sample = self._model.encode(["test"], show_progress_bar=False)
        if sample.shape[1] != EMBED_DIM:
            raise RuntimeError(
                f"模型维度 {sample.shape[1]} != 预期 {EMBED_DIM}，"
                f"db schema 需要重建"
            )
        logger.info("Model loaded. Dim = {}", EMBED_DIM)

    def encode(self, texts: List[str], batch_size: int = 32,
               show_progress: bool = False) -> np.ndarray:
        """编码一批文本。返回 shape=(len(texts), EMBED_DIM) 的 float32 数组。"""
        self._ensure_loaded()
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        # bge-m3 推荐 normalize_embeddings=True，方便后续用余弦相似度
        embs = self._model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
            convert_to_numpy=True,
        )
        return embs.astype(np.float32, copy=False)


_encoder_instance: Optional[_EncoderSingleton] = None


def get_encoder() -> _EncoderSingleton:
    global _encoder_instance
    if _encoder_instance is None:
        _encoder_instance = _EncoderSingleton()
    return _encoder_instance
