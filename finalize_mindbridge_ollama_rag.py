from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path.cwd()
BACKUP_DIR = ROOT / "target" / f"rag-finalization-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

FILES = [
    ROOT / "app" / "services" / "vector_store.py",
    ROOT / "README.md",
    ROOT / ".env.example",
]


def backup(path: Path) -> None:
    target = BACKUP_DIR / path.relative_to(ROOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def patch_vector_store() -> None:
    path = ROOT / "app" / "services" / "vector_store.py"
    text = path.read_text(encoding="utf-8")

    old_import = (
        "        try:\n"
        "            import chromadb\n"
        "        except ImportError as exc:\n"
    )
    new_import = (
        "        try:\n"
        "            import chromadb\n"
        "            from chromadb.config import Settings as ChromaSettings\n"
        "        except ImportError as exc:\n"
    )
    if "from chromadb.config import Settings as ChromaSettings" not in text:
        text = replace_once(text, old_import, new_import, "Chroma Settings import")

    old_client = "            self.client = chromadb.PersistentClient(path=str(self.persist_dir))\n"
    new_client = (
        "            self.client = chromadb.PersistentClient(\n"
        "                path=str(self.persist_dir),\n"
        "                settings=ChromaSettings(anonymized_telemetry=False),\n"
        "            )\n"
    )
    if "anonymized_telemetry=False" not in text:
        text = replace_once(text, old_client, new_client, "PersistentClient telemetry setting")

    path.write_text(text, encoding="utf-8", newline="\n")


def replace_matching_line(lines: list[str], needle: str, replacement: str) -> None:
    matches = [i for i, line in enumerate(lines) if needle in line]
    if len(matches) != 1:
        raise RuntimeError(f"README line {needle!r}: expected 1 match, found {len(matches)}")
    lines[matches[0]] = replacement


def replace_section(text: str, start_heading: str, end_heading: str, replacement: str) -> str:
    start = text.find(start_heading)
    if start < 0:
        raise RuntimeError(f"README start heading not found: {start_heading}")
    end = text.find(end_heading, start)
    if end < 0:
        raise RuntimeError(f"README end heading not found: {end_heading}")
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:]


def patch_readme() -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    replace_matching_line(
        lines,
        "* Chroma 向量 RAG 知识库：",
        "* Chroma 向量 RAG 知识库：支持 OpenAI 或 Ollama Embeddings；当前 Docker 默认使用 "
        "`nomic-embed-text:latest`，与 BM25 关键词召回融合后进入本地 reranker；向量不可用时保留 "
        "BM25 + `hybrid_score` 降级链路。",
    )
    replace_matching_line(
        lines,
        "RAG：本地知识库切块、OpenAI Embeddings",
        "RAG：本地知识库切块、OpenAI/Ollama Embeddings、Chroma 向量库、BM25、分数融合、本地 reranker、上下文扩展",
    )

    text = "\n".join(lines) + "\n"

    chroma_section = r"""## Chroma 向量库与快照

应用启动时会同步 `app/knowledge/*.md` 内置默认知识库到数据库。默认知识覆盖校园心理支持、风险等级、焦虑恐慌、情绪低落、睡眠作息、学业压力、考试季、人际关系、新生适应、咨询转介和隐私边界等主题。

知识库支持两个 embedding provider：

```text
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
→ OLLAMA_BASE_URL
→ OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest
→ Ollama /api/embed

KNOWLEDGE_EMBEDDING_PROVIDER=openai
→ OPENAI_BASE_URL
→ OPENAI_API_KEY
→ OPENAI_EMBEDDING_MODEL=text-embedding-3-small
→ OpenAI-compatible /embeddings
```

Docker 部署推荐配置：

```env
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest

OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true

CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=mindbridge_knowledge
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
CHROMA_SNAPSHOT_KEEP=5
EMBEDDING_TIMEOUT_SECONDS=120
```

主检索链路：

```text
Ollama/OpenAI embedding
→ Chroma 向量召回
+ BM25 关键词召回
→ 配置权重融合
→ 本地 reranker
→ 相邻知识分块扩展
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，如果 Chroma 或当前 embedding provider 不可用，系统自动降级到：

```text
BM25 + hybrid_score reranker
```

设为 `true` 时，向量初始化或调用失败会直接暴露错误。

不同 provider 和模型使用独立 Chroma collection。collection identity 包含 provider 和模型哈希，数据库 `embedding_json` 同时记录 provider、model 和 dimension，因此不会把 OpenAI 与 Ollama 向量混合。显式执行重建接口时，会使用当前 provider 重新生成全部知识向量。

管理员接口：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/knowledge/status
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/rebuild-vector
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/backup
```

正常 Ollama 状态应包含：

```text
vectorAvailable: true
embeddingProvider: ollama
embeddingModel: nomic-embed-text:latest
embeddingDimension: 768
vectorChunks: 与 databaseChunks 一致
vectorError: 空
```
"""
    text = replace_section(text, "## Chroma 向量库与快照", "## 工具队列、限流与死信", chroma_section)

    text = text.replace(
        "知识库向量检索也使用同一个 `OPENAI\\_API\\_KEY` 调用 embeddings API。相关配置：",
        "知识库向量检索可独立选择 `ollama` 或 `openai` provider。OpenAI 路线仍使用 "
        "`OPENAI_API_KEY`；Ollama 路线不需要 OpenAI Key。相关配置：",
    )
    text = text.replace(
        "当 `KNOWLEDGE\\_VECTOR\\_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 BM25 + `hybrid\\_score` reranker。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE\\_VECTOR\\_REQUIRED=true`。",
        "当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，Chroma 或当前 embedding provider 不可用不会阻断聊天，"
        "系统会回退到本地 BM25 + `hybrid_score` reranker。若交付验收要求必须走向量检索，可设置 "
        "`KNOWLEDGE_VECTOR_REQUIRED=true`。",
    )

    path.write_text(text, encoding="utf-8", newline="\n")



def patch_env_example() -> None:
    path = ROOT / ".env.example"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    updates = {
        "KNOWLEDGE_EMBEDDING_PROVIDER": "ollama",
        "OLLAMA_EMBEDDING_MODEL": "nomic-embed-text:latest",
        "EMBEDDING_TIMEOUT_SECONDS": "120",
    }

    seen: set[str] = set()
    for index, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0]
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            seen.add(key)

    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    missing = [str(path.relative_to(ROOT)) for path in FILES if not path.exists()]
    if missing:
        raise SystemExit(f"Run from the MindBridge project root. Missing: {missing}")

    for path in FILES:
        backup(path)

    patch_vector_store()
    patch_readme()
    patch_env_example()

    print("MindBridge RAG finalization patch applied.")
    print(f"Backup directory: {BACKUP_DIR}")
    print("Changed:")
    print("  - app/services/vector_store.py")
    print("  - README.md")
    print("  - .env.example")
    print("No database, Chroma data, Docker volume, report code, or harness code was changed.")


if __name__ == "__main__":
    main()
