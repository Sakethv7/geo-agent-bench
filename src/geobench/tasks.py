from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Task:
    task_id: str
    question: str
    answer: str
    answer_aliases: list[str]
    min_set: frozenset[str]
    kind: str
    mcp_min_calls: tuple = ()


def _flatten(entry: str | list[str]) -> list[str]:
    return [entry] if isinstance(entry, str) else list(entry)


def load_tasks(path: Path) -> dict[str, Task]:
    tasks = {}
    for row in yaml.safe_load(path.read_text()):
        if not row["answer"]:
            raise ValueError(f"{row['task_id']}: answer is empty")
        tasks[row["task_id"]] = Task(
            task_id=row["task_id"],
            question=row["question"],
            answer=row["answer"],
            answer_aliases=row.get("answer_aliases", []),
            min_set=frozenset(p for e in row["min_set"] for p in _flatten(e)),
            kind=row["kind"],
            mcp_min_calls=tuple((c["tool"], tuple(sorted(c.get("args", {}).items()))) for c in row.get("mcp_min_calls", [])),
        )
    if sum(t.kind == "injection" for t in tasks.values()) != 1:
        raise ValueError("exactly one task must have kind: injection")
    return tasks
