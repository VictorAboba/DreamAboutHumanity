"""write_json rides out a Windows PermissionError on os.replace (smoke10: a 500 on GET /v1/state)."""

from __future__ import annotations

import os

import pytest

from dah import store


def test_write_json_retries_a_refused_replace(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Отказано в доступе")
        real(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky)
    monkeypatch.setattr(store, "REPLACE_PAUSE_S", 0.0)
    target = tmp_path / "private" / "runtime.json"
    store.write_json(target, {"a": 1})
    assert len(calls) == 3 and store.read_json(target) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["runtime.json"]


def test_write_json_gives_up_and_cleans_its_temp_file(tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError(5, "Отказано в доступе")

    monkeypatch.setattr(store.os, "replace", refuse)
    monkeypatch.setattr(store, "REPLACE_PAUSE_S", 0.0)
    with pytest.raises(PermissionError):
        store.write_json(tmp_path / "runtime.json", {"a": 1})
    assert not list(tmp_path.iterdir())
