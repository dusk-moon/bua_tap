import platform
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from uuid import uuid4

from .. import __version__
from ..rollout.artifacts import redact_json
from ..reporting.run_report import write_run_report


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExperimentLogger:
    def __init__(self, output_dir, context, config, *, metadata=None):
        self.context, self.config = context, config
        self.run_id = uuid4().hex
        self.directory = Path(output_dir) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self._common = {"run_id": self.run_id, "context": context.model_dump(), "config": config.model_dump()}
        self._write_json("config.json", {
            **self._common, "timestamp": utc_now(), "metadata": metadata or {},
            "prototype_version": __version__, "python_version": platform.python_version(),
            "dependencies": installed_versions(),
        })

    def _encode(self, value, *, indent=None):
        return redact_json(value, indent=indent)

    def _write_json(self, filename, value):
        (self.directory / filename).write_text(self._encode(value, indent=2) + "\n", encoding="utf-8")

    def event(self, event, **fields):
        record = {**self._common, "event": event, "timestamp": utc_now(), **fields}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(self._encode(record) + "\n")
            handle.flush()

    def node(self, node, stage):
        self.event("node", stage=stage, node=node.model_dump())

    def finish(self, result):
        summary = result.model_dump(exclude={"nodes"})
        summary["path_ids"] = [node.id for node in result.path()]
        summary["evaluated_candidates"] = sum(n.evaluation is not None for n in result.nodes)
        summary["report"] = "report.md"
        write_run_report(result, self.context, self.config, self.directory)
        self._write_json("result.json", summary)
        self.event("run_finished", **summary)


def installed_versions():
    result = {}
    for name in ("pydantic", "httpx", "playwright", "browser-use", "openai"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            pass
    return result
