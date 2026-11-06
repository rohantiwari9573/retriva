"""Runs baseline_benchmark.py against several code versions under matching conditions.

Runs on the host with the standard library only. See docs/benchmarks/METHODOLOGY.md.

Rules it enforces, because an earlier ad-hoc run broke all of them:
- Output goes to a new directory, and every file is created exclusively, so a surviving
  process can't overwrite results.
- One instance at a time, through a lock holding the owner's PID.
- If free memory drops below --min-free-gb it stops safely and records why, leaving a
  result marked incomplete instead of continuing under pressure.

    python backend/scripts/controlled_benchmark.py --out-root docs/benchmarks \
      --variant old=../bench/old --variant new=../bench/new --rounds 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = "retriva-controlled-bench"
SERVICES = ["postgres", "redis", "minio", "stub-ai"]
LIMITS = {
    "VERIFY_RATE_LIMIT_LOGIN": "1000000",
    "VERIFY_RATE_LIMIT_LOGIN_ACCOUNT": "1000000",
    "VERIFY_RATE_LIMIT_LOGIN_ACCOUNT_SOFT": "1000000",
    "VERIFY_RATE_LIMIT_REGISTER": "1000000",
    "VERIFY_RATE_LIMIT_RETRIEVAL": "1000000",
    "VERIFY_RATE_LIMIT_UPLOAD": "1000000",
}


class MemoryPressure(Exception):
    pass


def run(args: list[str], cwd: Path | None = None, timeout: int = 900) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env={**os.environ, **LIMITS},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def free_memory_gb() -> float:
    if sys.platform == "win32":
        out = run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory",
            ]
        )
        return int(out.strip()) / 1024 / 1024
    with open("/proc/meminfo", encoding="ascii") as handle:
        info = dict(line.split(":", 1) for line in handle)
    return int(info["MemAvailable"].split()[0]) / 1024 / 1024


def write_once(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)


def compose(cwd: Path, *args: str, timeout: int = 900) -> str:
    return run(
        [
            "docker", "compose", "-p", PROJECT,
            "-f", "docker-compose.yml", "-f", "docker-compose.isolated.yml",
            *args,
        ],
        cwd=cwd,
        timeout=timeout,
    )


class StatsSampler:
    """Polls docker stats while a measurement runs."""

    def __init__(self, interval: float = 5.0) -> None:
        self.samples: list[dict[str, dict[str, float]]] = []
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                out = run(
                    [
                        "docker", "stats", "--no-stream",
                        "--filter", f"name={PROJECT}",
                        "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}",
                    ],
                    timeout=30,
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
            sample = {}
            for line in out.splitlines():
                name, cpu, memory = line.split("|")
                used = memory.split("/")[0].strip()
                value, unit = float(used[:-3]), used[-3:]
                megabytes = value * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(unit, 1)
                service = name.removeprefix(f"{PROJECT}-").rsplit("-", 1)[0]
                sample[service] = {"cpu_percent": float(cpu.rstrip("%")), "mem_mb": megabytes}
            self.samples.append(sample)

    def __enter__(self) -> StatsSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=40)

    def summary(self) -> dict[str, dict[str, float]]:
        services = {service for sample in self.samples for service in sample}
        result = {}
        for service in sorted(services):
            cpu = [s[service]["cpu_percent"] for s in self.samples if service in s]
            memory = [s[service]["mem_mb"] for s in self.samples if service in s]
            result[service] = {
                "samples": len(cpu),
                "cpu_percent_mean": round(sum(cpu) / len(cpu), 1),
                "cpu_percent_max": round(max(cpu), 1),
                "mem_mb_max": round(max(memory), 1),
            }
        return result


def benchmark(cwd: Path, *flags: str) -> dict:
    out = compose(
        cwd,
        "exec", "-T", "backend",
        "python", "-m", "scripts.baseline_benchmark",
        "--base-url", "http://localhost:8000",
        *flags,
    )
    return json.loads(out)


def check_memory(minimum: float) -> None:
    free = free_memory_gb()
    if free < minimum:
        raise MemoryPressure(f"{free:.2f} GB free, below the {minimum} GB limit")


def run_variant(name: str, cwd: Path, run_dir: Path, round_number: int, minimum: float) -> None:
    prefix = f"round{round_number}-{name}"
    compose(cwd, "down", "-v", "--remove-orphans")
    compose(cwd, "up", "-d", "--wait", "--no-deps", *SERVICES)
    # The old code has no migrate service, so both are migrated the same way here.
    compose(cwd, "run", "--rm", "--no-deps", "-T", "backend", "alembic", "upgrade", "head")
    compose(cwd, "up", "-d", "--wait", "--no-deps", "backend", "worker")
    time.sleep(10)

    check_memory(minimum)
    with StatsSampler() as sampler:
        started = time.time()
        seed = benchmark(cwd, "--seed-and-ingest")
    write_once(
        run_dir / f"{prefix}-1-ingestion.json",
        json.dumps({**seed, "resources": sampler.summary(), "seconds": time.time() - started}, indent=1),
    )
    state = json.dumps(seed["state"])

    # Cold: Postgres and the API restarted, so their caches are empty. The OS page
    # cache inside the Docker VM can't be dropped from here, so this isn't fully cold.
    compose(cwd, "restart", "postgres", "backend")
    compose(cwd, "up", "-d", "--wait", "--no-deps", "backend")
    check_memory(minimum)
    with StatsSampler() as sampler:
        cold = benchmark(cwd, "--measure", "--state", state, "--label", "cold")
    write_once(
        run_dir / f"{prefix}-2-cold.json",
        json.dumps({**cold, "resources": sampler.summary()}, indent=1),
    )

    # Warm-up: same workload, thrown away.
    check_memory(minimum)
    benchmark(cwd, "--measure", "--state", state, "--label", "warmup")

    check_memory(minimum)
    with StatsSampler() as sampler:
        warm = benchmark(cwd, "--measure", "--state", state, "--label", "warm")
    write_once(
        run_dir / f"{prefix}-3-warm.json",
        json.dumps({**warm, "resources": sampler.summary()}, indent=1),
    )

    compose(cwd, "stop", "redis")
    down = benchmark(cwd, "--login-probe", "--email", seed["state"]["email"])
    write_once(run_dir / f"{prefix}-4-redis-down.json", json.dumps(down, indent=1))
    compose(cwd, "down", "-v", "--remove-orphans")


def environment(variants: dict[str, Path]) -> dict:
    return {
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "free_memory_gb_at_start": round(free_memory_gb(), 2),
        },
        "docker": json.loads(
            run(["docker", "info", "--format", "{{json .}}"])
        ).get("ServerVersion"),
        "docker_vm": run(["docker", "info", "--format", "{{.NCPU}} cpus, {{.MemTotal}} bytes"]).strip(),
        "other_containers": run(["docker", "ps", "--format", "{{.Names}}"]).split(),
        "variants": {
            name: run(["git", "rev-parse", "HEAD"], cwd=path).strip()
            for name, path in variants.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", required=True, type=Path)
    parser.add_argument("--variant", action="append", required=True, metavar="NAME=PATH")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--min-free-gb", type=float, default=1.0)
    args = parser.parse_args()

    variants = {
        name: Path(path).resolve() for name, path in (v.split("=", 1) for v in args.variant)
    }
    source = Path(__file__).with_name("baseline_benchmark.py")
    for path in variants.values():
        target = path / "backend" / "scripts" / "baseline_benchmark.py"
        if target.resolve() != source.resolve():
            shutil.copyfile(source, target)

    lock = args.out_root / ".controlled_benchmark.lock"
    args.out_root.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"Another run holds {lock} (PID {lock.read_text()}). Not starting.", file=sys.stderr)
        return 3
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.out_root / f"run-{stamp}"
    run_dir.mkdir()
    status = {"completed": [], "stopped_early": None}
    try:
        write_once(run_dir / "environment.json", json.dumps(environment(variants), indent=1))
        names = list(variants)
        for round_number in range(1, args.rounds + 1):
            # Rotating the order keeps one version from always running first.
            shift = (round_number - 1) % len(names)
            for name in names[shift:] + names[:shift]:
                print(f"round {round_number}: {name}", flush=True)
                run_variant(name, variants[name], run_dir, round_number, args.min_free_gb)
                status["completed"].append(f"round{round_number}-{name}")
    except MemoryPressure as exc:
        status["stopped_early"] = f"memory pressure: {exc}"
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        status["stopped_early"] = f"error: {str(exc)[:1500]}"
    finally:
        for path in variants.values():
            try:
                compose(path, "down", "-v", "--remove-orphans")
                break
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
        write_once(run_dir / "status.json", json.dumps(status, indent=1))
        lock.unlink(missing_ok=True)
    print(json.dumps(status, indent=1))
    print(f"Results: {run_dir}")
    return 0 if status["stopped_early"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
