from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

import backend.config as config_mod
from backend.config import get_config, load_config


def _write_yaml(path: Path, projects: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"projects": projects}, f)


def _reset_config():
    config_mod._config = None
    config_mod._config_path = None
    config_mod._config_mtime = 0.0


SAMPLE_PROJECT = {
    "id": "test-proj",
    "name": "Test Project",
    "path": "/tmp/test-proj",
}


def test_load_config(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg = load_config(cfg_path)
    assert len(cfg.projects) == 1
    assert cfg.projects[0].id == "test-proj"


def test_get_config_returns_cached(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg1 = load_config(cfg_path)
    cfg2 = get_config()
    assert cfg1 is cfg2


def test_hot_reload_on_file_change(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg1 = load_config(cfg_path)
    assert len(cfg1.projects) == 1
    assert cfg1.projects[0].name == "Test Project"

    # Ensure mtime changes (some filesystems have 1s resolution)
    time.sleep(0.05)
    new_mtime = os.path.getmtime(cfg_path) + 1
    _write_yaml(cfg_path, [{**SAMPLE_PROJECT, "name": "Updated Project"}])
    os.utime(cfg_path, (new_mtime, new_mtime))

    cfg2 = get_config()
    assert cfg2.projects[0].name == "Updated Project"
    assert cfg2 is not cfg1


def test_no_reload_when_unchanged(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg1 = load_config(cfg_path)
    cfg2 = get_config()
    assert cfg1 is cfg2


def test_reload_picks_up_new_projects(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg1 = load_config(cfg_path)
    assert len(cfg1.projects) == 1

    new_mtime = os.path.getmtime(cfg_path) + 1
    second_project = {"id": "proj2", "name": "Project 2", "path": "/tmp/proj2"}
    _write_yaml(cfg_path, [SAMPLE_PROJECT, second_project])
    os.utime(cfg_path, (new_mtime, new_mtime))

    cfg2 = get_config()
    assert len(cfg2.projects) == 2
    assert cfg2.projects[1].id == "proj2"


def test_reload_with_dispatcher_config(tmp_path):
    """Legacy dispatcher config in YAML is silently ignored."""
    cfg_path = tmp_path / "projects.yaml"
    proj_with_dispatcher = {
        **SAMPLE_PROJECT,
        "dispatcher": {"enabled": True, "command": "python dispatch.py"},
    }
    _write_yaml(cfg_path, [proj_with_dispatcher])
    _reset_config()

    cfg = load_config(cfg_path)
    assert cfg.projects[0].id == "test-proj"
    assert not hasattr(cfg.projects[0], "dispatcher")


def test_graceful_when_file_deleted(tmp_path):
    cfg_path = tmp_path / "projects.yaml"
    _write_yaml(cfg_path, [SAMPLE_PROJECT])
    _reset_config()

    cfg1 = load_config(cfg_path)
    os.remove(cfg_path)

    # Should return cached config when file is gone
    cfg2 = get_config()
    assert cfg2 is cfg1
