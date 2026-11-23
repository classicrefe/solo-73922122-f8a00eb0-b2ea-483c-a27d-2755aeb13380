"""HTTP-level tests for the Flask review page."""

import io

import pytest

from app import create_app
from app.demo import LABEL_CUT, TOTAL_BOTTLES, demo_csv


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "web.sqlite3"))
    app.testing = True
    with app.test_client() as client:
        yield client
    app.extensions["relabel_store"].close()


def upload(client, csv_text, total=TOTAL_BOTTLES, label=LABEL_CUT):
    return client.post(
        "/api/import",
        data={
            "file": (io.BytesIO(csv_text.encode()), "review.csv"),
            "total_bottles": str(total),
            "label_cut": str(label),
        },
        content_type="multipart/form-data",
    )


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "重贴标签核对" in response.get_data(as_text=True)


def test_demo_import_and_summary(client):
    response = upload(client, demo_csv())
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    summary = payload["summary"]
    assert summary["initialized"] is True
    assert summary["candidates"]["first"] == 58_400
    assert summary["candidates"]["last"] == 58_887
    assert summary["must"] is None
    assert summary["bounds"] == {"min": 0, "max": 320}
    assert summary["counts"]["partial"] == 487


def test_single_edit_changes_relabel_range(client):
    upload(client, demo_csv())
    before = client.get("/api/summary").get_json()["summary"]
    response = client.post(
        "/api/sample", json={"bottle": 58_800, "verdict": "old"}
    )
    assert response.status_code == 200
    payload = response.get_json()
    after = payload["summary"]
    assert after["must"] == {"start": 58_721, "end": 58_800, "count": 80}
    assert after["partial"] == {"start": 58_801, "end": 58_887, "count": 87}
    assert after["bounds"] == {"min": 80, "max": 167}
    assert before["must"] is None

    # undo the fix: the report must return exactly to the committed state
    client.post("/api/sample", json={"bottle": 58_800, "verdict": "pending"})
    restored = client.get("/api/summary").get_json()["summary"]
    assert restored["must"] == before["must"]
    assert restored["partial"] == before["partial"]
    assert restored["bounds"] == before["bounds"]


def test_label_correction_changes_report(client):
    upload(client, demo_csv())
    response = client.post("/api/label", json={"label_cut": 58_500})
    assert response.status_code == 200
    summary = response.get_json()["summary"]
    assert summary["label_cut"] == 58_500
    client.post("/api/sample", json={"bottle": 58_800, "verdict": "old"})
    fixed = client.get("/api/summary").get_json()["summary"]
    assert fixed["must"] == {"start": 58_501, "end": 58_800, "count": 300}
    assert fixed["partial"] == {"start": 58_801, "end": 58_887, "count": 87}
    assert fixed["bounds"] == {"min": 300, "max": 387}


def test_bad_csv_rejected_and_previous_state_kept(client):
    upload(client, demo_csv())
    committed = client.get("/api/summary").get_json()["summary"]
    response = upload(client, "bottle,verdict\n1,old\n1,new\n")
    assert response.status_code == 400
    assert "重复" in response.get_json()["error"]
    assert client.get("/api/summary").get_json()["summary"] == committed


def test_bad_label_and_out_of_range_bottle(client):
    upload(client, demo_csv())
    assert client.post("/api/label", json={"label_cut": 999999}).status_code == 400
    assert client.post("/api/sample", json={"bottle": 0, "verdict": "old"}).status_code == 400
    assert client.post("/api/sample", json={"bottle": 10, "verdict": "maybe"}).status_code == 400


def test_csv_aliases_and_gb18030(client):
    csv_text = "瓶号,判定\n1,旧\n2,新\n3,待核\n"
    response = upload(client, csv_text, total=10, label=1)
    assert response.status_code == 200
    summary = response.get_json()["summary"]
    assert summary["state"]["samples_old"] == 1
    assert summary["state"]["samples_new"] == 1
    assert summary["state"]["samples_pending"] == 1


def test_import_without_label_then_set_it(client):
    response = upload(client, demo_csv(), label="")
    assert response.status_code == 200
    summary = response.get_json()["summary"]
    assert summary["label_missing"] is True
    response = client.post("/api/label", json={"label_cut": LABEL_CUT})
    assert response.get_json()["summary"]["label_missing"] is False
