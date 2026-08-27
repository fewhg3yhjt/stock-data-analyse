"""Versioned storage for operation-points strategy definitions and runs."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import yaml


class OperationPointStore:
    def __init__(self, root: Path | str | None = None):
        if root is None:
            root = Path(__file__).resolve().parent.parent / "schemes" / "operation_points"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = self.root / ".versions"
        self.versions.mkdir(exist_ok=True)
        self.runs = self.root / "runs.jsonl"

    def save(self, config: dict) -> dict:
        name = str(config.get("name") or "operation_points_default").strip()
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("策略名只能包含字母、数字、下划线和连字符")
        payload = {"strategy_type": "operation_points_v1", **config, "name": name}
        path = self.root / f"{name}.yaml"
        content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        history = self.versions / f"{name}.json"
        entries = json.loads(history.read_text(encoding="utf-8")) if history.exists() else []
        entries.append({"version": str(payload.get("version", "1.0")), "created_at": time.time(), "hash": digest, "config": payload})
        history.write_text(json.dumps(entries[-50:], ensure_ascii=False, indent=2), encoding="utf-8")
        return {"name": name, "version": str(payload.get("version", "1.0")), "hash": digest, "path": str(path)}

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            result.append({"name": data.get("name", path.stem), "version": str(data.get("version", "1.0")), "strategy_type": data.get("strategy_type", "operation_points_v1"), "description": data.get("description", ""), "config": data})
        return result

    def record_run(self, *, name: str, version: str, code: str, data_as_of: str | None, result: dict) -> dict:
        item = {"created_at": time.time(), "name": name, "version": version, "code": code, "data_as_of": data_as_of, "result": result}
        with self.runs.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return item
