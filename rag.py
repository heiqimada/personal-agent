"""日记向量检索（V1）。

Chunking（chunk_size=500、overlap=50）+ 本地 Ollama bge-m3 embedding，
向量与元数据持久化到 data/chroma_db/ 的 "journal_chunks" 集合。
用法参考了本机 ~/Desktop/my-rag/ 的 build_index.py，但 embedding 按
任务要求统一走 OpenAI SDK 的本地 Ollama 兼容端点。

使用前先执行：ollama pull bge-m3
"""

import os

import chromadb
from openai import OpenAI


# ===== 路径与集合配置 =====
# 相对本文件定位项目根目录，保证从任意工作目录运行都能找到 data/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.path.join(BASE_DIR, "data", "chroma_db")  # ChromaDB 持久化目录
COLLECTION_NAME = "journal_chunks"  # 存放日记切片的向量集合名

# ===== 切片参数（与 ~/Desktop/my-rag 参考实现一致） =====
CHUNK_SIZE = 500    # 每块最大字符数
CHUNK_OVERLAP = 50  # 相邻两块的重叠字符数，保住边界上下文

# ===== Embedding 配置：本地 Ollama（OpenAI 兼容端点） =====
EMBED_BASE_URL = "http://localhost:11434/v1"
EMBED_MODEL = "bge-m3"        # 中文向量模型，需 ollama pull bge-m3
EMBED_API_KEY = "ollama"      # Ollama 不校验 Key，按 OpenAI 规范填占位值


# 模块级缓存：Embedding 客户端与 Chroma 集合都只初始化一次
_client = None
_collection = None


def _get_embedding_client() -> OpenAI:
    """惰性创建 OpenAI SDK 客户端（指向本地 Ollama 的 /v1 端点）。"""
    global _client
    if _client is None:
        # 本地调用也留足超时，避免大文本向量化时被中断
        _client = OpenAI(
            base_url=EMBED_BASE_URL,
            api_key=EMBED_API_KEY,
            timeout=120,
        )
    return _client


def _get_collection():
    """惰性打开（必要时创建）ChromaDB 持久化集合。"""
    global _collection
    if _collection is None:
        # PersistentClient：向量数据落盘到 data/chroma_db/，重启不丢失
        chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
        try:
            # 集合已存在时直接复用，避免重复创建
            _collection = chroma_client.get_collection(COLLECTION_NAME)
        except Exception:
            # 首次运行才创建；与 my-rag 参考实现一致采用余弦距离
            _collection = chroma_client.create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
    return _collection


def split_text(text, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP):
    """按字符切块，相邻块之间保留 overlap 个字符的重叠。"""
    text = (text or "").strip()  # 容错空值，并去掉首尾空白
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]  # 短文本无需切分，直接整段入库

    step = max(1, chunk_size - chunk_overlap)  # 每次前进的步长 = 块长 - 重叠
    chunks = []
    for start in range(0, len(text), step):
        piece = text[start:start + chunk_size].strip()
        # 跳过空片段，并避免重叠滑动时把同一段重复入块
        if not piece or (chunks and piece == chunks[-1]):
            continue
        chunks.append(piece)
    return chunks


def embed_texts(texts):
    """把多条文本向量化，返回与输入顺序一致的 embedding 列表。"""
    if not texts:
        return []
    # 一次请求批量编码全部文本，减少本地 Ollama 往返
    response = _get_embedding_client().embeddings.create(
        model=EMBED_MODEL,
        input=texts,
    )
    # 按返回的 index 排序，确保与传入顺序一一对应
    ordered = sorted(response.data, key=lambda item: item.index)
    return [item.embedding for item in ordered]


def add_journal(journal_id, text, title="", written_at="", source="manual"):
    """切片 → 逐条 embedding → 入库。metadata 带 journal_id/title/written_at/source。

    参数：
        journal_id：日记主键（向量切片 id 的一部分，重复导入靠它保持稳定）；
        text：实际切片入库的正文（调用方决定是否拼 title）；
        title：日记标题，冗余存一份便于排查；
        written_at：日记落笔日期，由导入链路传入，缺省空字符串；
        source：日记来源（如 manual / apple_notes/自我思考记），缺省 manual。
    返回：实际入库的切片数（空正文返回 0）。

    为什么默认值写死空字符串而不是 None：ChromaDB 的 metadata 值
    不允许 None（只接受 str/int/float/bool），缺失字段必须给 ""，
    否则入库时会抛异常或写入脏值。
    """
    written_at = written_at or ""   # 防御：调用方传 None 也归一为空串，防 Chroma 报错
    source = source or ""

    chunks = split_text(text)
    if not chunks:
        return 0  # 空正文不入库

    # 1) 逐块向量化
    embeddings = embed_texts(chunks)
    # 2) 一次性写入向量集合
    collection = _get_collection()
    # upsert：同一 journal 重复导入时按稳定 id 覆盖旧切片
    collection.upsert(
        ids=[f"journal_{journal_id}_chunk_{i}" for i in range(len(chunks))],
        documents=chunks,
        embeddings=embeddings,
        metadatas=[
            {
                "journal_id": journal_id,  # 检索结果据此回溯到日记主键
                "title": title,            # 冗余标题便于排查与展示
                # written_at/source 由 /api/import 透传进来；
                # 空值一律用 ""，理由见函数 docstring（Chroma 禁 None）
                "written_at": written_at,
                "source": source,
                "chunk_index": i,          # 同一篇日记内的切片序号
            }
            for i in range(len(chunks))
        ],
    )
    return len(chunks)  # 返回实际入库的切片数，便于日志确认


def search(query, top_k=3):
    """query embedding → ChromaDB 检索。

    返回：[{"content", "journal_id", "written_at", "source", "distance"}, ...]
    （按相关度升序）；written_at/source 从 metadata 读取，缺失给空字符串，
    保证调用方（tools.py 抬头拼接）不需要做 None 分支。
    """
    collection = _get_collection()
    count = collection.count()
    if count == 0:
        return []  # 集合为空时直接返回空结果，避免 Chroma 报错

    n_results = min(top_k, count)  # 请求条数不能超过库内切片总数
    query_embedding = embed_texts([query])[0]  # 先把用户问题转成向量
    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],  # 需要正文、元数据、距离
    )

    hits = []
    # Chroma 返回按 query 分组的多层结构，这里取第一条（也是唯一一条）查询的结果
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]
    for content, meta, distance in zip(documents, metadatas, distances):
        hits.append(
            {
                "content": content,
                "journal_id": meta.get("journal_id"),
                "written_at": meta.get("written_at") or "",
                "source": meta.get("source") or "",
                "distance": distance,
            }
        )
    return hits
