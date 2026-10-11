"""Bounded process execution, independent of dataset semantics and cache keys.

Workers inherit the caller's process group so a web job's group cancellation
also stops its children. Only compact, picklable task descriptions should be
submitted; never send a loaded DatasetSource or open codec to a worker.
"""

from itertools import chain, islice
import multiprocessing
import os
from queue import Empty, Queue


def worker_count(requested, task_count):
    """Resolve auto concurrency from available CPUs minus two, capped at twelve.

    An explicit positive integer overrides the automatic CPU limit, but cannot
    create more workers than tasks. Zero tasks need no worker. Booleans are not
    accepted as integers. Execution settings intentionally do not affect caches.
    """
    if requested is not None and (type(requested) is not int or requested < 1):
        raise ValueError("workers must be a positive integer or None for automatic selection")
    if type(task_count) is not int or task_count < 0:
        raise ValueError("task_count must be a nonnegative integer")
    if task_count == 0:
        return 0
    if requested is None:
        try:
            cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cpus = os.cpu_count() or 1
        requested = min(12, max(1, cpus - 2))
    return min(requested, task_count)


def iter_parallel(function, items, workers=None):
    """Yield ``(original_index, result)`` as bounded tasks finish.

    The callable and its inputs must be picklable under multiprocessing ``spawn``.
    At most ``workers`` tasks are submitted at a time; iterable inputs are consumed
    incrementally. A one-worker run calls the function in this process. Task
    exceptions retain their type and message; a worker exiting without a result
    raises RuntimeError. An exception or closing this iterator terminates and
    joins its pool, including running tasks, before returning control. Callers
    stopping early must close the iterator (for example with closing()).
    """
    # Resolve the resource limit before consuming an iterable. Prefetching at
    # most that many items also avoids starting unnecessary workers for a small
    # input without materializing a potentially large collection.
    limit = worker_count(workers, 2**63 - 1)
    remaining = enumerate(items)
    initial = list(islice(remaining, limit))
    limit = min(limit, len(initial))
    if not limit:
        return
    if limit == 1:
        for index, item in chain(initial, remaining):
            yield index, function(item)
        return

    completed = Queue()
    pool = multiprocessing.get_context("spawn").Pool(processes=limit)
    finished = False
    try:
        # Pool silently replaces a process killed by e.g. a codec segfault, but
        # that task never gets a result/error callback. Retain the initial
        # Process handles so timed queue waits can detect the lost task. Pool
        # has no public worker-access API: this single read-only snapshot is
        # the only private-API dependency; lifecycle operations remain public.
        # No maxtasksperchild is set, so any exit before shutdown is unexpected.
        processes = tuple(pool._pool)

        def submit(index, item):
            # Callbacks execute in the parent's result thread, not the worker;
            # the thread-safe queue itself never needs to be pickled.
            pool.apply_async(
                function, (item,),
                callback=lambda value, i=index: completed.put((i, True, value)),
                error_callback=lambda error, i=index: completed.put((i, False, error)),
            )

        for index, item in initial:
            submit(index, item)
        pending = len(initial)
        while pending:
            try:
                index, success, value = completed.get(timeout=0.1)
            except Empty:
                exited = [(process.pid, process.exitcode) for process in processes
                          if process.exitcode is not None]
                if exited:
                    details = ", ".join(f"PID {pid}, exit code {code}" for pid, code in exited)
                    raise RuntimeError(f"Worker process exited without completing its task ({details})")
                continue
            pending -= 1
            if not success:
                raise value
            yield index, value
            # Refill only when the consumer resumes after the yielded result,
            # so closing the iterator does not launch extra work.
            item = next(remaining, None)
            if item is not None:
                submit(*item)
                pending += 1
        finished = True
    finally:
        try:
            if finished:
                pool.close()
            else:
                pool.terminate()
        finally:
            pool.join()
