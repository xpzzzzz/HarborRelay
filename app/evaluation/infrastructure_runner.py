"""Run real cache/vector probes in owned ephemeral containers, using cached images."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.evaluation.mysql_runner import local_docker_context


def owned_container(docker, identifier, run_id):
    marker = subprocess.check_output(
        [
            *docker,
            "inspect",
            identifier,
            "--format",
            '{{index .Config.Labels "harborrelay.validation-run"}}',
        ],
        text=True,
        timeout=10,
    ).strip()
    if marker != run_id:
        raise RuntimeError(
            "Refusing to mutate a container without this run's ownership label"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-image", default="mindbridge-py-app:latest")
    parser.add_argument("--redis-image", default="redis:7.2-alpine")
    parser.add_argument("--output-dir", default="target/infrastructure-validation")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex[:12]
    evidence = output / run_id
    evidence.mkdir()
    docker = None
    containers = []
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "passed": False,
        "cleanup_succeeded": False,
        "evidence_directory": str(evidence),
        "scope": "New owned containers; scratch tmpfs; readonly source/skills; no host DB/data mounts",
        "app_image": args.app_image,
        "redis_image": args.redis_image,
    }
    try:
        docker = local_docker_context()
        for name, image in [("app", args.app_image), ("redis", args.redis_image)]:
            report[f"{name}_image_id"] = json.loads(
                subprocess.check_output(
                    [*docker, "image", "inspect", image, "--format", "{{json .Id}}"],
                    text=True,
                    timeout=10,
                )
            )
        labels = [
            "--label",
            f"harborrelay.validation-run={run_id}",
            "--label",
            "harborrelay.synthetic=true",
        ]
        redis_id = subprocess.check_output(
            [
                *docker,
                "run",
                "--pull=never",
                "--rm",
                "-d",
                "--name",
                f"harborrelay-redis-probe-{run_id}",
                *labels,
                "--read-only",
                "--tmpfs",
                "/data:rw,size=64m",
                "--tmpfs",
                "/tmp:rw,size=16m",
                "--memory",
                "128m",
                args.redis_image,
                "redis-server",
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--protected-mode",
                "yes",
            ],
            text=True,
            timeout=25,
        ).strip()
        containers.append(redis_id)
        owned_container(docker, redis_id, run_id)
        ping = subprocess.run(
            [*docker, "exec", redis_id, "redis-cli", "ping"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ping.returncode or ping.stdout.strip() != "PONG":
            raise RuntimeError("Owned Redis container did not become ready")
        container_name = f"harborrelay-vector-probe-{run_id}"
        # Start idle so ownership is checked before executing code or cleanup; no app startup.
        worker_id = subprocess.check_output(
            [
                *docker,
                "run",
                "--pull=never",
                "--rm",
                "-d",
                "--name",
                container_name,
                *labels,
                "--network",
                f"container:{redis_id}",
                "--read-only",
                "--tmpfs",
                "/scratch:rw,size=256m",
                "--tmpfs",
                "/probe-venv:rw,exec,size=128m",
                "--tmpfs",
                "/tmp:rw,size=128m",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--mount",
                f"type=bind,src={root / 'app'},dst=/workspace/app,readonly",
                "--mount",
                f"type=bind,src={root / 'skills'},dst=/workspace/skills,readonly",
                "--mount",
                f"type=bind,src={root / 'requirements.txt'},dst=/workspace/requirements.txt,readonly",
                "--mount",
                f"type=bind,src={evidence},dst=/evidence",
                "--workdir",
                "/workspace",
                "--env",
                "PYTHONPATH=/workspace",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                f"HARBORRELAY_INFRA_RUN={run_id}",
                "--env",
                "ANONYMIZED_TELEMETRY=False",
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--entrypoint",
                "python",
                args.app_image,
                "-c",
                "import time; time.sleep(900)",
            ],
            text=True,
            timeout=25,
        ).strip()
        containers.append(worker_id)
        owned_container(docker, worker_id, run_id)
        # The cached image predates the security patch. Install current requirements
        # into an ephemeral venv; never omit authentication code to make probes run.
        subprocess.run(
            [
                *docker,
                "exec",
                worker_id,
                "python",
                "-m",
                "venv",
                "--system-site-packages",
                "/probe-venv",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        setup = subprocess.run(
            [
                *docker,
                "exec",
                worker_id,
                "/probe-venv/bin/python",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "-r",
                "/workspace/requirements.txt",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        (evidence / "dependency-setup.txt").write_bytes(
            (setup.stdout + setup.stderr).encode("utf-8")
        )
        if setup.returncode:
            raise RuntimeError(
                "Ephemeral dependency setup failed; see dependency-setup.txt"
            )
        for command, filename in [
            ("check", "pip-check.txt"),
            ("freeze", "pip-freeze.txt"),
        ]:
            dependency_result = subprocess.run(
                [
                    *docker,
                    "exec",
                    worker_id,
                    "/probe-venv/bin/python",
                    "-m",
                    "pip",
                    command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            (evidence / filename).write_bytes(
                (dependency_result.stdout + dependency_result.stderr).encode("utf-8")
            )
            if dependency_result.returncode:
                raise RuntimeError(
                    f"Ephemeral dependency validation failed; see {filename}"
                )
        report["dependency_check_passed"] = True
        print(
            f"Running synthetic Redis/Chroma probes in owned containers ({run_id})",
            flush=True,
        )
        result = subprocess.run(
            [
                *docker,
                "exec",
                worker_id,
                "/probe-venv/bin/python",
                "-m",
                "app.evaluation.infrastructure_probe",
                "--run-id",
                run_id,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=240,
        )
        (evidence / "stdout.txt").write_bytes(result.stdout.encode("utf-8"))
        (evidence / "stderr.txt").write_bytes(result.stderr.encode("utf-8"))
        report["probe_exit_code"] = result.returncode
        if (evidence / "probe.json").is_file():
            probe = json.loads((evidence / "probe.json").read_text("utf-8"))
            report["checks"] = probe["checks"]
            report["app_source_sha256"] = probe["app_source_sha256"]
            report["passed"] = probe["passed"] and result.returncode == 0
        else:
            report["error"] = "Probe produced no structured result"
    except Exception as exc:
        report["error"] = type(exc).__name__
        report["detail"] = str(exc)[:1000]
    finally:
        cleanup = []
        for identifier in reversed(containers):
            try:
                owned_container(docker, identifier, run_id)
                subprocess.run(
                    [*docker, "stop", "--time", "5", identifier],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                cleanup.append({"id": identifier, "removed": True})
            except Exception as exc:
                cleanup.append(
                    {"id": identifier, "removed": False, "error": type(exc).__name__}
                )
        report["cleanup"] = cleanup
        report["cleanup_succeeded"] = bool(cleanup) and all(
            item["removed"] for item in cleanup
        )
        (output / "report.json").write_bytes(
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] and report["cleanup_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
