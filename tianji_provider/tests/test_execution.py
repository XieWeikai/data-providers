"""Resource bounds and cleanup for spawned provider jobs (no MCAP required)."""

from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tianji_provider import execution


def _worker_info(value):
    return value, os.getpid(), os.getpgid(0), multiprocessing.get_start_method()


def _gated_task(item):
    root, index = item
    root = Path(root)
    (root / f"started-{index}").write_text(str(os.getpid()))
    if index == 0:
        deadline = time.monotonic() + 15
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Test gate was not released")
            time.sleep(0.01)
    return index


def _fail_or_wait(index):
    if index == 0:
        raise ValueError("worker failed deliberately")
    time.sleep(15)
    return index


def _children():
    return {process.pid for process in multiprocessing.active_children()}


@pytest.mark.parametrize("requested", [True, False, 0, -1, 1.5, "2"])
def test_invalid_worker_requests(requested):
    with pytest.raises(ValueError, match="workers"):
        execution.worker_count(requested, 5)
    with pytest.raises(ValueError, match="workers"):
        list(execution.iter_parallel(abs, [], workers=requested))


@pytest.mark.parametrize("count", [True, -1, 1.5, "2"])
def test_invalid_task_counts(count):
    with pytest.raises(ValueError, match="task_count"):
        execution.worker_count(2, count)


def test_explicit_workers_and_empty_input():
    assert execution.worker_count(20, 3) == 3
    assert execution.worker_count(20, 30) == 20
    assert execution.worker_count(1, 30) == 1
    assert execution.worker_count(None, 0) == 0
    assert execution.worker_count(4, 0) == 0
    assert list(execution.iter_parallel(abs, (), workers=4)) == []


def test_auto_uses_affinity_and_caps_at_eight(monkeypatch):
    monkeypatch.setattr(execution.os, "sched_getaffinity", lambda _: set(range(14)), raising=False)
    monkeypatch.setattr(execution.os, "cpu_count", lambda: 128)
    assert execution.worker_count(None, 100) == 8
    assert execution.worker_count(None, 3) == 3
    monkeypatch.setattr(execution.os, "sched_getaffinity", lambda _: set(range(6)))
    assert execution.worker_count(None, 100) == 4
    monkeypatch.setattr(execution.os, "sched_getaffinity", lambda _: {0})
    assert execution.worker_count(None, 100) == 1


def test_auto_falls_back_when_affinity_unavailable(monkeypatch):
    monkeypatch.delattr(execution.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(execution.os, "cpu_count", lambda: 6)
    assert execution.worker_count(None, 100) == 4
    monkeypatch.setattr(execution.os, "cpu_count", lambda: None)
    assert execution.worker_count(None, 100) == 1
    def unavailable(_):
        raise OSError("affinity unavailable")
    monkeypatch.setattr(execution.os, "sched_getaffinity", unavailable, raising=False)
    monkeypatch.setattr(execution.os, "cpu_count", lambda: 5)
    assert execution.worker_count(None, 100) == 3


def test_one_worker_and_single_item_do_not_create_a_pool(monkeypatch):
    def unexpected_pool(_):
        raise AssertionError("serial execution must not create a process pool")
    monkeypatch.setattr(execution.multiprocessing, "get_context", unexpected_pool)
    assert list(execution.iter_parallel(lambda item: (item, os.getpid()), [1, 2], workers=1)) == [
        (0, (1, os.getpid())), (1, (2, os.getpid()))]
    assert list(execution.iter_parallel(abs, [-3], workers=8)) == [(0, 3)]
    assert list(execution.iter_parallel(abs, [], workers=8)) == []
    with pytest.raises(ValueError, match="worker failed deliberately"):
        list(execution.iter_parallel(_fail_or_wait, [0], workers=1))


def test_spawned_workers_inherit_process_group_and_are_joined():
    before = _children()
    results = list(execution.iter_parallel(_worker_info, [10, 20, 30], workers=2))
    assert sorted((index, value) for index, (value, _, _, _) in results) == [(0, 10), (1, 20), (2, 30)]
    for _, (_, pid, pgid, method) in results:
        assert pid != os.getpid()
        assert pgid == os.getpgid(0)
        assert method == "spawn"
    assert _children() <= before


def test_completion_order_and_bounded_iterable_consumption(tmp_path):
    consumed = []
    def items():
        for index in range(3):
            consumed.append(index)
            yield str(tmp_path), index
    before = _children()
    with closing(execution.iter_parallel(_gated_task, items(), workers=2)) as results:
        assert next(results) == (1, 1)
        assert consumed == [0, 1]
        assert not (tmp_path / "started-2").exists()
        assert next(results) == (2, 2)
        (tmp_path / "release").touch()
        assert list(results) == [(0, 0)]
    assert consumed == [0, 1, 2]
    assert _children() <= before


def test_worker_error_terminates_other_running_tasks():
    before = _children()
    started = time.monotonic()
    with pytest.raises(ValueError, match="worker failed deliberately"):
        list(execution.iter_parallel(_fail_or_wait, [0, 1], workers=2))
    # The other task sleeps 15 seconds; error cleanup must not wait for it.
    assert time.monotonic() - started < 10
    assert _children() <= before


def test_early_close_terminates_running_tasks_without_refilling(tmp_path):
    before = _children()
    results = execution.iter_parallel(_gated_task, [(str(tmp_path), i) for i in range(3)], workers=2)
    try:
        assert next(results) == (1, 1)
        started = time.monotonic()
        results.close()
        assert time.monotonic() - started < 5
    finally:
        results.close()
    assert not (tmp_path / "started-2").exists()
    assert _children() <= before


def test_input_error_terminates_running_tasks(tmp_path):
    def items():
        yield str(tmp_path), 0
        yield str(tmp_path), 1
        raise RuntimeError("input failed deliberately")
    before = _children()
    with pytest.raises(RuntimeError, match="input failed deliberately"):
        list(execution.iter_parallel(_gated_task, items(), workers=2))
    assert _children() <= before


@pytest.mark.parametrize("exit_code", [0, 7])
def test_abrupt_worker_exit_does_not_hang_or_leave_children(exit_code):
    # Run outside pytest's own process and impose an external timeout: without
    # the liveness check, Pool replaces exited workers but loses their pending
    # tasks, so this regression would otherwise hang the entire test suite.
    script = """
import multiprocessing
import os
import sys
from tianji_provider.execution import iter_parallel

if __name__ == '__main__':
    try:
        list(iter_parallel(os._exit, [int(sys.argv[1])] * 2, workers=2))
    except RuntimeError as error:
        assert 'exited without completing its task' in str(error), str(error)
        assert 'exit code ' + sys.argv[1] in str(error), str(error)
        assert not multiprocessing.active_children()
    else:
        raise AssertionError('Abrupt exit must be reported as failure')
"""
    with subprocess.Popen([sys.executable, "-c", script, str(exit_code)],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            # If the regression returns, kill the isolated test process group,
            # including Pool replacements, rather than leaving orphan workers.
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"Abrupt worker exit hung the pool: {stdout}\n{stderr}")
        assert process.returncode == 0, stdout + stderr
