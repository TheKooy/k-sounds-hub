
import json
import time

from ksound_hub.module_remote_bridge import (
    receipt_begin,
    receipt_read,
    receipt_finish,
    RECEIPT_TTL,
)


def test_receipts(tmp_path):
    a = receipt_begin(tmp_path)
    b = receipt_begin(tmp_path)

    assert a != b

    assert receipt_read(tmp_path, a)["state"] == "pending"

    assert receipt_finish(
        tmp_path,
        a,
        True,
        "OK",
    )

    assert receipt_read(tmp_path, a)["state"] == "accepted"

    assert not receipt_finish(
        tmp_path,
        a,
        False,
        "Overwrite",
    )

    assert receipt_finish(
        tmp_path,
        b,
        False,
        "Fail",
    )

    assert receipt_read(tmp_path, b)["state"] == "refused"

    assert receipt_read(
        tmp_path,
        "../../unsafe",
    ) is None

    path = tmp_path / (a + ".json")

    data = json.loads(
        path.read_text()
    )

    data["created"] = (
        time.time()
        - RECEIPT_TTL
        - 1
    )

    path.write_text(
        json.dumps(data)
    )

    assert receipt_read(tmp_path, a) is None
