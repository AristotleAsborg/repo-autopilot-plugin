"""3.2 的向量库：flat numpy + 余弦（路线：<5000 条用 flat，更大再上 pgvector）。

## 为什么是 numpy 而不是向量数据库

路线给的门槛是 5000 条。5000×1024 的矩阵是 20MB，一次全量余弦就是一次矩阵乘法，
在这台机器上是毫秒级 —— 上 pgvector 只会多一个要运维的东西、多一个会挂的环节。
门槛不是猜的：`tools/eval_dedupe.py` 里有一条 5000 条的实测，超过 2s 才需要换。

## 存储格式（两个文件，一起才是完整的库）

    state/vectors/issues.npy     (n, dim) float32，**已 L2 归一化**
    state/vectors/issues.jsonl   每行一个 {"key", "meta"}，行序与矩阵行序**一一对应**

为什么不用一个 npz 装完：人要看这个库的时候（"到底建了哪些 issue 的向量"），
jsonl 用记事本就能读。向量本身是二进制，本来就没人手读。
**行序对应关系是这个库唯一的隐性契约**：save/load 时必须整段写、整段读，
不允许中途插入或删除某一行 —— 行错位不会报错，只会让查重悄悄指向另一条 issue。
所以 `add()` 是按 key 去重的**追加**，真要删除就整库重建（重建很便宜）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "state" / "vectors"
VECTORS_FILE = "issues.npy"
META_FILE = "issues.jsonl"


class VectorStoreError(RuntimeError):
    """向量库自身的问题（维度不一致、行数对不上）。绝不静默降级。"""


@dataclass(frozen=True)
class Match:
    """一次检索的命中。score 是余弦相似度（向量已归一化，所以就是点积）。"""

    key: str
    score: float
    meta: dict = field(default_factory=dict)


class VectorStore:
    """内存里持有整库，查询走一次矩阵乘法。"""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else DEFAULT_DIR
        self._vectors: np.ndarray | None = None
        self._keys: list[str] = []
        self._meta: list[dict] = []
        self._index: dict[str, int] = {}

    # ------------------------------------------------------------------ 读
    @property
    def dim(self) -> int:
        return 0 if self._vectors is None else int(self._vectors.shape[1])

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def meta_of(self, key: str) -> dict:
        position = self._index.get(key)
        return dict(self._meta[position]) if position is not None else {}

    # ------------------------------------------------------------------ 写
    def add(self, key: str, vector: np.ndarray, **meta: object) -> bool:
        """
        追加一条。key 已存在时**跳过**（返回 False），不覆盖也不重复。

        为什么不覆盖：向量是 issue 正文的函数。正文改了应该走"整库重建"，
        而不是悄悄换掉一行 —— 后者会让"这条为什么被判重复"的追溯断掉。
        """
        if key in self._index:
            return False
        row = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        norm = float(np.linalg.norm(row))
        if norm == 0:
            raise VectorStoreError(f"{key} 是零向量，无法归一化（相似度会全变成 0）")
        row = row / norm
        if self._vectors is None:
            self._vectors = row
        else:
            if row.shape[1] != self._vectors.shape[1]:
                raise VectorStoreError(
                    f"维度不一致：库里是 {self._vectors.shape[1]}，{key} 是 {row.shape[1]}。"
                    "换了 embedding 模型就必须整库重建"
                )
            self._vectors = np.vstack([self._vectors, row])
        self._index[key] = len(self._keys)
        self._keys.append(key)
        self._meta.append(dict(meta))
        return True

    def add_many(self, keys: list[str], vectors: np.ndarray, metas: list[dict] | None = None) -> int:
        """批量追加，返回真正写进去的条数（已存在的会被跳过）。"""
        metas = metas or [{} for _ in keys]
        added = 0
        for position, key in enumerate(keys):
            if self.add(key, vectors[position], **metas[position]):
                added += 1
        return added

    # ------------------------------------------------------------------ 查
    def search(self, vector: np.ndarray, top_k: int = 1) -> list[Match]:
        """返回相似度最高的 top_k 条（降序）。空库返回空列表 —— 空库不是错误。"""
        if self._vectors is None or not self._keys:
            return []
        row = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(row))
        if norm == 0:
            raise VectorStoreError("查询向量是零向量")
        row = row / norm
        scores = self._vectors @ row
        order = np.argsort(-scores)[: max(1, top_k)]
        return [
            Match(key=self._keys[int(i)], score=float(scores[int(i)]), meta=dict(self._meta[int(i)]))
            for i in order
        ]

    # ------------------------------------------------------------- 落盘
    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        vectors = self._vectors if self._vectors is not None else np.zeros((0, 0), dtype=np.float32)
        np.save(self.directory / VECTORS_FILE, vectors)
        with open(self.directory / META_FILE, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps({"key": key, "meta": meta}, ensure_ascii=False) + "\n" for key, meta in zip(self._keys, self._meta, strict=True))

    @classmethod
    def load(cls, directory: Path | str | None = None) -> VectorStore:
        store = cls(directory)
        vectors_path = store.directory / VECTORS_FILE
        meta_path = store.directory / META_FILE
        if not vectors_path.exists() and not meta_path.exists():
            return store  # 还没建库，是正常状态
        vectors = np.load(vectors_path).astype(np.float32)
        keys: list[str] = []
        metas: list[dict] = []
        with open(meta_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                keys.append(payload["key"])
                metas.append(payload.get("meta") or {})
        # 行序契约：对不上就报错，绝不让"行错位"变成静默的错答案
        if vectors.shape[0] != len(keys):
            raise VectorStoreError(
                f"{VECTORS_FILE} 有 {vectors.shape[0]} 行，{META_FILE} 有 {len(keys)} 条 —— "
                "两者必须一一对应，请整库重建"
            )
        store._vectors = vectors
        store._keys = keys
        store._meta = metas
        store._index = {key: position for position, key in enumerate(keys)}
        return store
