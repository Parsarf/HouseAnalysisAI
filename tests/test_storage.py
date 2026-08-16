import pytest

from common.storage import LocalFilesystemStorage, S3Storage


def test_local_materialize_rejects_missing_document(tmp_path):
    storage = LocalFilesystemStorage(tmp_path)

    with (
        pytest.raises(FileNotFoundError, match="stored document does not exist"),
        storage.materialize(str(tmp_path / "missing.pdf")),
    ):
        pass


def test_s3_save_accepts_child_reference_without_double_prefix():
    storage = object.__new__(S3Storage)
    storage.bucket = "documents"

    class Client:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    storage.client = Client()
    child = storage.child("s3://documents/report/original.pdf", "units/0001.txt")

    reference = storage.save_text("hello", child)

    assert reference == "s3://documents/report/units/0001.txt"
    assert storage.client.calls == [
        {"Bucket": "documents", "Key": "report/units/0001.txt", "Body": b"hello"}
    ]
