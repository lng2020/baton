from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProjectConfig:
    id: str
    name: str
    path: str
    repo: str = ""
    description: str = ""
    color: str = "#0f3460"
    tasks_dir: str = "tasks"

    @property
    def project_path(self) -> Path:
        return Path(self.path)

    @property
    def tasks_path(self) -> Path:
        return self.project_path / self.tasks_dir


@dataclass
class BatonConfig:
    projects: list[ProjectConfig] = field(default_factory=list)


_config: BatonConfig | None = None


def load_config(path: str | Path | None = None) -> BatonConfig:
    global _config
    if path is None:
        path = Path(__file__).parent.parent / "config" / "projects.yaml"
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)
    projects = [ProjectConfig(**p) for p in raw.get("projects", [])]
    _config = BatonConfig(projects=projects)
    return _config


def get_config() -> BatonConfig:
    if _config is None:
        return load_config()
    return _config


def get_project_by_id(project_id: str) -> ProjectConfig | None:
    for p in get_config().projects:
        if p.id == project_id:
            return p
    return None
