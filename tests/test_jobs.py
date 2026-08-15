from jobs import InMemoryJobQueue, JobStatus


def test_queue_deduplicates_and_completes():
    queue = InMemoryJobQueue()
    first = queue.enqueue("demo", {"id": 1}, "same")
    assert queue.enqueue("demo", {"id": 2}, "same").id == first.id
    queue.run_once({"demo": lambda payload: None})
    assert first.status == JobStatus.COMPLETE


def test_queue_retries_then_dead_letters():
    queue = InMemoryJobQueue()
    job = queue.enqueue("bad", {})
    job.max_attempts = 2
    for _ in range(2):
        queue.run_once({"bad": lambda payload: (_ for _ in ()).throw(RuntimeError("boom"))})
    assert job.status == JobStatus.DEAD
