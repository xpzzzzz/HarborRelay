"""Container-only probes for real Redis, Chroma and local Ollama embeddings."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DOCUMENTS = {
    "synthetic-sleep": "Synthetic sleep schedule example: keep a regular bedtime and morning routine.",
    "synthetic-study": "Synthetic study example: organize assignments, revision and programming practice.",
    "synthetic-campus": "Synthetic campus example: library opening times and room reservations.",
}


def scope(run_id):
    if (
        not run_id
        or not run_id.isalnum()
        or os.environ.get("HARBORRELAY_INFRA_RUN") != run_id
        or not Path("/.dockerenv").exists()
    ):
        raise RuntimeError("Probe requires its own generated container scope")
    root = Path("/scratch") / run_id
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        DATABASE_URL=f"sqlite:///{root}/probe.sqlite3",
        AI_PROVIDER="mock",
        ALERT_EMAIL_DELIVERY_MODE="log",
        TOOL_QUEUE_ENABLED="false",
        KNOWLEDGE_VECTOR_ENABLED="false",
        ANONYMIZED_TELEMETRY="False",
    )
    return root


class EmbeddingGateway:
    """Fault injection forwards successful synthetic calls to the real local model."""

    def __init__(self):
        import httpx

        self.mode = "healthy"
        self.forwarded = 0
        self.model = "nomic-embed-text:latest"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

            def respond(self, method):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if owner.mode == "offline":
                    status, content = 503, b'{"error":"synthetic provider outage"}'
                elif owner.mode == "slow":
                    time.sleep(0.2)
                    status, content = 503, b'{"error":"synthetic timeout"}'
                elif (
                    owner.mode in {"wrong_dimension", "wrong_count", "non_finite"}
                    and self.path == "/api/embed"
                ):
                    payload = json.loads(body)
                    count = len(payload["input"])
                    vector = (
                        [1.0, 0.0]
                        if owner.mode == "wrong_dimension"
                        else [float("nan"), *([0.0] * 767)]
                    )
                    vectors = (
                        []
                        if owner.mode == "wrong_count"
                        else [vector for _ in range(count)]
                    )
                    status, content = 200, json.dumps({"embeddings": vectors}).encode()
                else:
                    if self.path not in {"/api/tags", "/api/embed"}:
                        self.send_error(404)
                        return
                    if method == "POST":
                        payload = json.loads(body)
                        if payload.get("model") != owner.model:
                            self.send_error(400)
                            return
                        owner.forwarded += 1
                    try:
                        response = httpx.request(
                            method,
                            "http://host.docker.internal:11434" + self.path,
                            content=body,
                            headers={"Content-Type": "application/json"},
                            timeout=30,
                            trust_env=False,
                        )
                        status, content = response.status_code, response.content
                    except httpx.HTTPError:
                        status, content = 502, b'{"error":"local provider unreachable"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def child(root, kind, config_file):
    from app.core.config import Settings
    from app.models.entities import KnowledgeChunk
    from app.services.vector_store import ChromaKnowledgeStore

    path = Path(config_file).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(
            "Child configuration must remain in this run's scratch directory"
        )
    data = json.loads(path.read_text())
    settings = Settings(_env_file=None, **data["settings"])
    for name in ["chroma_persist_dir", "chroma_snapshot_dir"]:
        if not Path(getattr(settings, name)).resolve().is_relative_to(root):
            raise RuntimeError(
                "Child vector paths must remain in the owned scratch directory"
            )
    store = (
        ChromaKnowledgeStore.restore_snapshot(data["snapshot"], settings)
        if kind == "restore"
        else ChromaKnowledgeStore(settings)
    )
    if data.get("expected_generation") is not None:
        rows = store.collection.get(include=["metadatas", "documents", "embeddings"])
        assert len(rows["ids"]) == data["expected_count"]
        assert {m["source"] for m in rows["metadatas"]} == {data["expected_generation"]}
        assert all(len(v) == 768 for v in rows["embeddings"])
        assert set(rows["ids"]) == {f"knowledge-chunk-{i}" for i in range(1000, 1300)}
        print(
            json.dumps(
                {
                    "count": store.count(),
                    "generation": data["expected_generation"],
                    "all_ids_and_dimensions_verified": True,
                }
            )
        )
        return
    if kind == "crash-write":
        store.upsert_chunks(
            [
                KnowledgeChunk(
                    id=99,
                    source="synthetic-crash",
                    source_index=0,
                    content="synthetic crash record",
                )
            ],
            [data["vector"]],
        )
        os._exit(75)
    hits = store.query(data["vector"], 1)
    assert store.count() == data["expected_count"]
    assert (
        store.embedding_dimension == 768 and hits[0].source == data["expected_source"]
    )
    print(
        json.dumps(
            {
                "count": store.count(),
                "dimension": store.embedding_dimension,
                "first_source": hits[0].source,
            }
        )
    )


def evaluate(root, run_id, evidence):
    import httpx
    import redis
    from sqlalchemy.orm import Session
    from app.agents.runtime import AgentContext, AgentRuntimeService
    from app.core.config import Settings
    from app.core.database import Base, engine
    from app.models.entities import (
        ChatMessage,
        ChatSession,
        KnowledgeChunk,
        ToolJob,
        UserAccount,
    )
    from app.schemas.dtos import AiMessage, ChatRequest
    from app.services.chat import ChatService
    from app.services.knowledge import KnowledgeService, parse_embedding
    from app.services.memory import RedisShortTermMemoryStore
    from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable

    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{root}/probe.sqlite3",
        ai_provider="mock",
        redis_url="redis://127.0.0.1:6379/0",
        redis_memory_max_messages=4,
        redis_memory_ttl_seconds=30,
        redis_socket_timeout_seconds=0.05,
        knowledge_embedding_provider="ollama",
        ollama_embedding_model="nomic-embed-text:latest",
        knowledge_vector_enabled=True,
        knowledge_vector_required=True,
        chroma_persist_dir=str(root / "chroma"),
        chroma_snapshot_dir=str(root / "snapshots"),
        chroma_snapshot_keep=2,
        chroma_collection_name=f"infra-{run_id}",
        embedding_timeout_seconds=30,
        knowledge_top_k=2,
        alert_email_delivery_mode="log",
        tool_queue_enabled=False,
        excel_path=str(root / "unused.xlsx"),
    )
    control = redis.Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=2
    )
    assert control.ping()
    gateway = EmbeddingGateway()
    settings.ollama_base_url = gateway.url
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "checks": [],
        "python": platform.python_version(),
        "redis_server": control.info("server")["redis_version"],
        "packages": {
            p: importlib.metadata.version(p)
            for p in [
                "redis",
                "chromadb",
                "numpy",
                "onnxruntime",
                "pydantic",
                "posthog",
                "mcp",
                "argon2-cffi",
            ]
        },
        "scope": "owned Redis namespace, scratch SQLite/Chroma, synthetic text and real local embeddings",
        "limits": [
            "No business database or sensitive data",
            "Logical snapshots serialize application threads in one writer process, not external Chroma clients",
            "App child process exit only, not Redis/Chroma server disaster recovery",
            "No real SMTP notifications",
        ],
    }
    digest = hashlib.sha256()
    for p in sorted(Path("/workspace/app").rglob("*.py")):
        digest.update(p.relative_to("/workspace").as_posix().encode())
        digest.update(p.read_bytes())
    report["app_source_sha256"] = digest.hexdigest()

    def save():
        report["passed"] = all(c["passed"] for c in report["checks"])
        (evidence / "probe.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def check(name, function):
        started = time.perf_counter()
        try:
            details = function()
            result = {"id": name, "passed": True, "details": details}
        except Exception as exc:
            result = {
                "id": name,
                "passed": False,
                "error": type(exc).__name__,
                "detail": str(exc)[-1000:],
            }
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        report["checks"].append(result)
        save()
        print(json.dumps(result, ensure_ascii=False), flush=True)

    memory = RedisShortTermMemoryStore(settings)

    def roundtrip():
        for index in range(7):
            memory.append(
                run_id, "USER", f"synthetic {index} 13800138000 example@example.test"
            )
        loaded = memory.load_recent(run_id)
        assert len(loaded) == 4 and loaded[-1].content.startswith("synthetic 6")
        assert all(m.role == "user" and "13800138000" not in m.content for m in loaded)
        assert 0 < control.ttl(memory._key(run_id)) <= 30
        assert "13800138000" not in "".join(control.lrange(memory._key(run_id), 0, -1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda i: memory.append(
                        run_id, "assistant", f"synthetic parallel {i}"
                    ),
                    range(32),
                )
            )
        assert control.llen(memory._key(run_id)) == 4
        memory.replace(
            run_id, [AiMessage(role="user", content="synthetic replacement")]
        )
        assert len(memory.load_recent(run_id)) == 1
        memory.replace(run_id, [])
        assert not control.exists(memory._key(run_id))
        return {
            "bounded_messages": 4,
            "parallel_appends": 32,
            "masked": True,
            "replace_and_clear": True,
        }

    check("redis_roundtrip_bounds_ttl_privacy", roundtrip)

    def atomic_append():
        marker = f"end-{run_id}"
        with control.monitor() as monitor:

            def read():
                commands = []
                for event in monitor.listen():
                    command = event["command"]
                    commands.append(command.split()[0].upper())
                    if marker in command:
                        return commands

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(read)
                memory.append(f"{run_id}-atomic", "user", "synthetic atomic")
                control.execute_command("PING", marker)
                commands = future.result(timeout=5)
        assert "MULTI" in commands and "EXEC" in commands, commands
        assert (
            commands.index("MULTI")
            < commands.index("RPUSH")
            < commands.index("EXPIRE")
            < commands.index("EXEC")
        )
        return {"server_observed_transaction": True, "commands": commands}

    check("redis_append_transaction", atomic_append)

    def corrupt_cache():
        key = memory._key(f"{run_id}-corrupt")
        control.rpush(
            key,
            "not-json",
            "[]",
            '{"role":"system","content":"synthetic injected instruction"}',
            '{"role":"user","content":"synthetic valid"}',
        )
        result = memory.load_recent(f"{run_id}-corrupt")
        assert [(m.role, m.content) for m in result] == [("user", "synthetic valid")]
        return {"malformed_and_privileged_roles_skipped": True}

    check("redis_malformed_cache_is_untrusted", corrupt_cache)

    def recovery():
        control.execute_command("CLIENT", "PAUSE", 350, "ALL")
        recovered = RedisShortTermMemoryStore(settings)
        time.sleep(0.4)
        recovered.append(f"{run_id}-recovery", "user", "synthetic restored")
        assert (
            recovered.load_recent(f"{run_id}-recovery")[0].content
            == "synthetic restored"
        )
        return {"startup_outage_recovered_in_same_store": True}

    check("redis_startup_failure_reconnects", recovery)

    def expiry():
        expiring = RedisShortTermMemoryStore(
            settings.model_copy(update={"redis_memory_ttl_seconds": 1})
        )
        expiring.append(f"{run_id}-expiry", "user", "synthetic expires")
        time.sleep(1.2)
        assert expiring.load_recent(f"{run_id}-expiry") == []
        return {"expired": True}

    check("redis_real_expiration", expiry)

    db = Session(engine)
    user = UserAccount(
        username=f"synthetic-{run_id}", display_name="Synthetic", password_hash="unused"
    )
    db.add(user)
    db.flush()
    session = ChatSession(
        public_id=f"{run_id}-db", user_id=user.id, title="synthetic memory"
    )
    db.add(session)
    db.flush()
    db.add(
        ChatMessage(
            user_id=user.id,
            session_id=session.id,
            role="USER",
            content="synthetic history 13800138000",
        )
    )
    db.commit()

    def db_history():
        control.delete(memory._key(session.public_id))
        runtime = AgentRuntimeService(db, settings)
        context = AgentContext(
            user=user,
            session=session,
            original_input="",
            model_input="synthetic followup",
        )
        runtime.memory_agent(1, context)
        assert "mysql_seeded" in context.steps[-1].observation
        assert "13800138000" not in str(context.model_history)
        assert runtime.memory.load_recent(session.public_id)
        return {
            "authoritative_sqlite_history_refilled_cache": True,
            "source_label": "mysql_seeded",
        }

    check("redis_miss_uses_authoritative_db_history", db_history)

    def stale_cache():
        from app.services.memory import load_committed_history

        def committed(content):
            row = ChatMessage(
                user_id=user.id, session_id=session.id, role="USER", content=content
            )
            db.add(row)
            db.commit()
            return row

        first, _ = load_committed_history(db, memory, session.id, session.public_id, 4)
        old_ids = [
            row[0]
            for row in db.query(ChatMessage.id)
            .filter_by(session_id=session.id)
            .order_by(ChatMessage.id)
        ]
        missed = committed("synthetic missed safety context: 我想自杀")
        control.execute_command("CLIENT", "PAUSE", 350, "ALL")
        memory.append(session.public_id, "USER", missed.content, message_id=missed.id)
        assert memory._retry_after > 0
        time.sleep(0.4)
        later = committed("synthetic later committed message")
        memory.append(session.public_id, "USER", later.content, message_id=later.id)
        loaded, source = load_committed_history(
            db, memory, session.id, session.public_id, 4
        )
        assert source == "mysql_seeded" and any(
            "我想自杀" in item.content for item in loaded
        )
        assert (
            load_committed_history(db, memory, session.id, session.public_id, 4)[1]
            == "redis_verified"
        )
        # A stale read-through writer can overwrite Redis; its IDs cannot validate.
        memory.replace(session.public_id, first, message_ids=old_ids)
        assert (
            len(load_committed_history(db, memory, session.id, session.public_id, 4)[0])
            == 3
        )
        # Out-of-order/duplicate delivery also cannot masquerade as a fresh window.
        memory.append(session.public_id, "USER", missed.content, message_id=missed.id)
        memory.append(session.public_id, "USER", later.content, message_id=later.id)
        assert (
            load_committed_history(db, memory, session.id, session.public_id, 4)[1]
            == "mysql_seeded"
        )
        return {
            "real_redis_append_timeout": True,
            "missing_middle_recovered": True,
            "stale_refill_rejected": True,
            "duplicate_delivery_rejected": True,
            "verified_cache_hit": True,
        }

    check("redis_committed_window_survives_lost_and_racing_writes", stale_cache)

    service = KnowledgeService(db, settings)

    # Keep cold model loading separate from the application embedding deadline.
    warm_started = time.perf_counter()
    inventory = httpx.get(
        "http://host.docker.internal:11434/api/tags", timeout=5, trust_env=False
    )
    inventory.raise_for_status()
    report["embedding_model"] = next(
        item
        for item in inventory.json()["models"]
        if item["name"] == settings.ollama_embedding_model
    )
    warm = httpx.post(
        "http://host.docker.internal:11434/api/embed",
        json={"model": settings.ollama_embedding_model, "input": ["synthetic warmup"]},
        timeout=120,
        trust_env=False,
    )
    warm.raise_for_status()
    warm_data = warm.json()
    report["embedding_warmup"] = {
        "latency_ms": (time.perf_counter() - warm_started) * 1000,
        "load_duration_ns": warm_data.get("load_duration"),
        "dimension": len(warm_data["embeddings"][0]),
    }
    save()

    def vectors():
        assert service.vector_store.can_embed, service.vector_store.error
        for source, text in DOCUMENTS.items():
            service.ingest(source, text)
        vector = service.vector_store.embed_texts([DOCUMENTS["synthetic-sleep"]])[0]
        assert len(vector) == 768
        assert service.vector_store.query(vector, 1)[0].source == "synthetic-sleep"
        assert (
            service.retrieve(DOCUMENTS["synthetic-sleep"])[0].source
            == "synthetic-sleep"
        )
        assert service.vector_store.count() == 3
        return {
            "indexed": 3,
            "embedding_dimension": 768,
            "exact_text_vector_and_hybrid_hit": True,
        }

    check("chroma_live_ollama_index_and_query", vectors)

    def provider_outage():
        gateway.mode = "offline"
        settings.knowledge_vector_required = False
        hits = service.retrieve(DOCUMENTS["synthetic-study"])
        assert (
            hits and hits[0].source == "synthetic-study" and service.vector_store.error
        )
        settings.knowledge_vector_required = True
        try:
            service.retrieve(DOCUMENTS["synthetic-study"])
        except Exception:
            pass
        else:
            raise AssertionError("Required vector mode silently fell back")
        gateway.mode = "slow"
        settings.knowledge_vector_required = False
        settings.embedding_timeout_seconds = 0.05
        timed = service.retrieve(DOCUMENTS["synthetic-study"])
        assert timed and "ReadTimeout" in service.vector_store.error
        settings.embedding_timeout_seconds = 30
        settings.knowledge_vector_required = True
        gateway.mode = "healthy"
        status = service.status()
        assert status["vectorAvailable"] is True, status["vectorError"]
        assert service.retrieve(DOCUMENTS["synthetic-study"])
        return {
            "optional_bm25_fallback": True,
            "required_raised": True,
            "status_recovered_same_service": True,
            "real_http_timeout_fallback": True,
        }

    check("chroma_provider_outage_and_recovery", provider_outage)
    gateway.mode = "healthy"
    settings.knowledge_vector_required = True

    def required_disabled():
        try:
            disabled = KnowledgeService(
                db, settings.model_copy(update={"knowledge_vector_enabled": False})
            )
            disabled.retrieve("synthetic study")
        except VectorStoreUnavailable:
            return {"required_vectors_never_silently_fallback": True}
        raise AssertionError(
            "Required/disabled vector configuration silently fell back"
        )

    check("chroma_required_disabled_fails_closed", required_disabled)

    def invalid_vectors():
        dimension = service.vector_store.embedding_dimension
        for value in [float("nan"), float("inf")]:
            vector = [0.0] * dimension
            vector[0] = value
            try:
                service.vector_store._validate_embeddings([vector], 1)
            except VectorStoreUnavailable:
                pass
            else:
                raise AssertionError("Non-finite embedding accepted")
            raw = json.dumps(
                {
                    "provider": "ollama",
                    "model": settings.ollama_embedding_model,
                    "dimension": dimension,
                    "vector": vector,
                }
            )
            assert (
                parse_embedding(raw, "ollama", settings.ollama_embedding_model) is None
            )
        return {"non_finite_provider_and_cached_vectors_rejected": True}

    check("chroma_rejects_non_finite_vectors", invalid_vectors)

    def invalid_http_vectors():
        for mode in ["wrong_count", "wrong_dimension", "non_finite"]:
            gateway.mode = mode
            try:
                service.vector_store.embed_texts(["synthetic invalid response probe"])
            except VectorStoreUnavailable:
                pass
            else:
                raise AssertionError(
                    f"Accepted invalid HTTP embedding response: {mode}"
                )
            assert service.vector_store.embedding_dimension == 768
        gateway.mode = "healthy"
        assert service.vector_store.count() == 3
        assert service.retrieve(DOCUMENTS["synthetic-sleep"])
        return {
            "bad_count_dimension_and_nan_rejected_before_upsert": True,
            "index_unchanged": True,
        }

    check("chroma_rejects_invalid_http_embeddings", invalid_http_vectors)
    gateway.mode = "healthy"

    def persistence():
        store = service.vector_store
        vector = store.embed_texts([DOCUMENTS["synthetic-sleep"]])[0]

        def invoke(kind, path, expected, source):
            data = {
                "settings": settings.model_dump(),
                "vector": vector,
                "expected_count": expected,
                "expected_source": source,
            }
            data["settings"].update(
                chroma_persist_dir=str(path),
                chroma_snapshot_dir=str(root / "child-snapshots"),
            )
            config = root / f"child-{kind}.json"
            config.write_text(json.dumps(data))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.evaluation.infrastructure_probe",
                    "--run-id",
                    run_id,
                    "--child",
                    kind,
                    "--config",
                    str(config),
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            (evidence / f"child-{kind}-{path.name}.stderr.txt").write_text(
                result.stderr, encoding="utf-8"
            )
            assert result.returncode == (75 if kind == "crash-write" else 0), (
                result.stderr[-1000:]
            )
            return result

        invoke("reopen", store.persist_dir, 3, "synthetic-sleep")
        snapshot = Path(store.snapshot())
        restored = root / "restored"
        ChromaKnowledgeStore.restore_snapshot(
            snapshot, settings.model_copy(update={"chroma_persist_dir": str(restored)})
        )
        invoke("reopen", restored, 3, "synthetic-sleep")
        crash_path = root / "crash-vector"
        invoke("crash-write", crash_path, 1, "synthetic-crash")
        invoke("reopen", crash_path, 1, "synthetic-crash")
        assert len(list((root / "snapshots").iterdir())) <= 2
        return {
            "fresh_process_reopen": True,
            "quiescent_snapshot_restore": True,
            "app_child_exit_code": 75,
            "crash_write_reopened": True,
            "snapshot_retention": 2,
        }

    check("chroma_reopen_snapshot_and_process_exit", persistence)

    def concurrent_snapshot():
        from unittest.mock import patch
        from app.services.vector_snapshots import validate_snapshot

        config = settings.model_copy(
            update={
                "chroma_persist_dir": str(root / "busy-chroma"),
                "chroma_snapshot_dir": str(root / "busy-snapshots"),
            }
        )
        busy = ChromaKnowledgeStore(config)
        second = ChromaKnowledgeStore(config)
        vector = service.vector_store.embed_texts(["synthetic concurrent snapshot"])[0]

        def generation(name):
            return [
                KnowledgeChunk(
                    id=i,
                    source=name,
                    source_index=i - 1000,
                    content=f"synthetic {name} {i}",
                )
                for i in range(1000, 1300)
            ]

        busy.upsert_chunks(generation("generation-0"), [vector] * 300)
        export_entered, release_export, write_attempted, write_done = [
            threading.Event() for _ in range(4)
        ]
        original_get = busy.collection.get

        def paused_get(**kwargs):
            if kwargs.get("offset") == 0 and not export_entered.is_set():
                export_entered.set()
                assert release_export.wait(10), "snapshot release timed out"
            return original_get(**kwargs)

        def write():
            write_attempted.set()
            second.upsert_chunks(generation("generation-1"), [vector] * 300)
            write_done.set()

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(busy.collection, "get", side_effect=paused_get),
        ):
            exporting = pool.submit(busy.snapshot)
            assert export_entered.wait(10)
            writing = pool.submit(write)
            try:
                assert write_attempted.wait(5)
                assert not write_done.wait(0.1), "writer bypassed snapshot lock"
            finally:
                release_export.set()
            snapshot = Path(exporting.result(timeout=30))
            writing.result(timeout=30)
        assert write_done.is_set()
        assert validate_snapshot(snapshot).count == 300
        data = {
            "settings": config.model_dump(),
            "snapshot": str(snapshot),
            "expected_count": 300,
            "expected_generation": "generation-0",
            "vector": vector,
        }
        data["settings"]["chroma_persist_dir"] = str(root / "busy-restored")
        child_config = root / "busy-restore.json"
        child_config.write_text(json.dumps(data))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.evaluation.infrastructure_probe",
                "--run-id",
                run_id,
                "--child",
                "restore",
                "--config",
                str(child_config),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        (evidence / "child-busy-restore.stdout.txt").write_text(result.stdout)
        (evidence / "child-busy-restore.stderr.txt").write_text(result.stderr)
        assert result.returncode == 0, result.stderr[-1000:]
        # Tampering, incomplete export and overwrite attempts must fail closed.
        damaged = root / "damaged-snapshot"
        shutil.copytree(snapshot, damaged)
        with (damaged / "records.jsonl").open("ab") as output:
            output.write(b"{}\n")
        try:
            validate_snapshot(damaged)
        except ValueError:
            pass
        else:
            raise AssertionError("Damaged snapshot accepted")
        try:
            ChromaKnowledgeStore.restore_snapshot(snapshot, config)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Restore overwrote active directory")
        return {
            "concurrent_writer_waited": True,
            "store_instances": 2,
            "paginated_records": 300,
            "restored_in_fresh_process": True,
            "snapshot_generation": "generation-0",
            "live_generation": "generation-1",
            "all_ids_dimensions_verified": True,
            "corruption_and_active_overwrite_rejected": True,
        }

    check(
        "chroma_busy_logical_snapshot_restores_one_complete_generation",
        concurrent_snapshot,
    )

    def identity_and_sync():
        other = ChromaKnowledgeStore(
            settings.model_copy(
                update={"ollama_embedding_model": "synthetic-other-model"}
            )
        )
        assert (
            other.collection_name != service.vector_store.collection_name
            and other.count() == 0
        )
        db.query(KnowledgeChunk).filter_by(source="synthetic-campus").delete()
        db.commit()
        assert service.rebuild_vector_index() == 2
        assert service.vector_store.count() == 2
        service.ingest("synthetic-study", DOCUMENTS["synthetic-study"])
        assert service.vector_store.count() == 2
        return {
            "model_isolated": True,
            "stale_vector_removed": True,
            "source_reingest_idempotent": True,
        }

    check("chroma_model_identity_and_rebuild", identity_and_sync)

    def urgent_path():
        gateway.mode = "offline"
        before = gateway.forwarded
        control.execute_command("CLIENT", "PAUSE", 5000, "ALL")
        chat = ChatService(db, settings)

        async def run():
            stream = chat.stream_chat(
                user, ChatRequest(message="我现在想伤害自己，synthetic safety probe")
            )
            first = await anext(stream)
            assert "event: meta" in first and '"outputMode": "safety_template"' in first
            assert db.query(ToolJob).count() == 3
            events = [event async for event in stream]
            assert any("event: done" in event for event in events)

        asyncio.run(run())
        assert gateway.forwarded == before
        assert chat.agent_harness.memory._retry_after > 0
        return {
            "high_risk_skipped_vector_provider": True,
            "outbox_jobs_before_meta": 3,
            "mock_ai_only": True,
            "real_redis_outage_observed": True,
        }

    check("high_risk_outbox_survives_provider_outage", urgent_path)
    try:
        response = httpx.get(
            "http://host.docker.internal:11434/api/tags", timeout=5, trust_env=False
        )
        report["embedding_model"] = next(
            item
            for item in response.json()["models"]
            if item["name"] == settings.ollama_embedding_model
        )
    except Exception as exc:
        report["model_inventory_error"] = type(exc).__name__
    report["live_embedding_requests"] = gateway.forwarded
    gateway.close()
    control.close()
    db.close()
    engine.dispose()
    save()
    return report["passed"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--child", choices=["reopen", "crash-write", "restore"])
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    root = scope(args.run_id)
    if args.child:
        child(root, args.child, args.config)
        return 0
    return 0 if evaluate(root, args.run_id, Path("/evidence")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
