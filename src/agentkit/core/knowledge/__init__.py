"""知识图谱能力（agentkit 内置）。

- ``ingest``  : 通用 PDF → Neo4j 知识图谱入库（``kg-ingest``）。
- ``graph``   : Neo4j 图谱问答客户端（快照/搜索/只读 Cypher/端到端问答）。
- ``seeder``  : 灌图器（seed/clear/count），配套领域数据见 ``seed_data``。

典型用法::

    from agentkit.core.knowledge import run_ingest, build_hlm_client
"""

from __future__ import annotations

from agentkit.core.knowledge.graph import HlmKgClient, build_hlm_client
from agentkit.core.knowledge.ingest import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SCHEMA,
    IngestReport,
    chunk_text,
    read_pdf,
    run_ingest,
)
from agentkit.core.knowledge.seeder import clear, count, run, seed

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_SCHEMA",
    "HlmKgClient",
    "IngestReport",
    "build_hlm_client",
    "chunk_text",
    "clear",
    "count",
    "read_pdf",
    "run",
    "run_ingest",
    "seed",
]
