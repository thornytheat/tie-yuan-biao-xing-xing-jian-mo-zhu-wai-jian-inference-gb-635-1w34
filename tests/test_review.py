"""M8 契约裁定与发布：两套依据并列、冲突标记、逐条裁定、整版发布与重放。

全部断言用样例数据算出的确定值，不读 datetime.now()，不用随机数。
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from app import config, errors
from app.modules.m5_batches import service as batch_service
from app.modules.m8_review import candidates, service
from app.store import Store
from fixtures import expected as X

REGION_ID = candidates.relation_id("odl_store", ["region_code"],
                                   "odl_region", ["region_code"])
PAYMENT_ID = candidates.relation_id("odl_payment", ["order_id"],
                                   "odl_sale_order", ["order_id"])


@pytest.fixture()
def review(store, settings, batch_id) -> dict:
    return service.current(store, settings, batch_id)


def _accept_all(client, payload: dict) -> dict:
    review_id = payload["review"]["review_id"]
    decisions = [{"item_id": item["item_id"], "decision": "accept"}
                 for item in payload["items"]]
    response = client.post(f"/api/review/{review_id}/decide",
                           json={"decisions": decisions})
    assert response.status_code == 200
    return response.json()


def _mutate_payment_sample(sample_dir, old: str, new: str) -> None:
    """只改 odl_payment 段：样本证据只在这一段变化，其余表原样。"""
    path = sample_dir / "sample_dump.csv"
    text = path.read_text(encoding="utf-8-sig")
    start = text.index("## table=odl_payment")
    tail = text.index("## table=", start + 1)
    segment = text[start:tail]
    assert old in segment
    path.write_text(text[:start] + segment.replace(old, new, 1) + text[tail:],
                    encoding="utf-8-sig")


# ---------- 两套依据 ----------

def test_declared_only_relation_kept_even_when_sample_disagrees(review):
    """TRAP-21：DDL 声明 store→region，样本包含度只有 0.75，声明依据不能消失。"""
    items = {item["item_id"]: item for item in review["items"]}
    region = items[REGION_ID]
    assert region["conflict_type"] == "declared_only"
    assert region["ddl"]["status"] == "unidirectional"
    assert region["ddl"]["inclusion"] == X.REGION_INCLUSION
    # 样本依据也要留：是「采到了但不支撑」，不是「没有证据」
    assert region["sample"]["status"] == "unsupported"
    assert region["sample"]["inclusion"] == X.REGION_INCLUSION
    assert region["ddl"] is not None and region["sample"] is not None


def test_sample_only_relation_enters_review_not_speculation(review):
    """样本证实的 payment→sale_order 在 DDL 里没声明：进裁定包，不直接进规格。"""
    items = {item["item_id"]: item for item in review["items"]}
    payment = items[PAYMENT_ID]
    assert payment["conflict_type"] == "sample_only"
    assert payment["ddl"] is None
    assert payment["sample"]["status"] == "inferred"
    assert payment["sample"]["inclusion"] == X.PAYMENT_INCLUSION
    assert payment["sample"]["cardinality_ratio"] == X.PAYMENT_CARDINALITY
    assert payment["decision"] == "pending"


def test_conflict_forms_and_counts(review):
    """样例得到 6 条两侧一致、1 条声明独有、1 条样本独有、0 条同列异父。"""
    assert review["conflicts"] == {"mismatch": 0, "declared_only": 1,
                                   "sample_only": 1, "agreement": 6}
    assert len(review["items"]) == X.DECLARED_FK + X.INFERRED_FK
    assert review["decisions"]["pending"] == len(review["items"])
    assert review["ready"] is False


def test_both_sides_snapshotted_on_agreement(review):
    """两侧一致的条目同样保留两份依据快照，数值逐字一致。"""
    items = {item["item_id"]: item for item in review["items"]}
    store_rel = items[candidates.relation_id("odl_sale_order", ["store_code"],
                                             "odl_store", ["store_code"])]
    assert store_rel["conflict_type"] == "agreement"
    assert store_rel["ddl"]["inclusion"] == "1.0000"
    assert store_rel["sample"]["inclusion"] == "1.0000"
    assert store_rel["basis_note"].startswith("声明 confirmed")


def test_mismatch_when_two_sides_point_to_different_parents():
    """同列异父：同一子列声明指向 A，样本却证实指向 B，整组标 mismatch 并给出另见。"""
    declared = [{"kind": "fk", "child_table": "t_item", "child_columns": ["order_id"],
                 "parent_table": "t_order", "parent_columns": ["order_id"],
                 "declared": True, "status": "unidirectional",
                 "inclusion": "0.5000", "cardinality_ratio": "1.0000",
                 "reason_code": "K_FK_UNIDIRECTIONAL", "note": ""}]
    samples = [{"kind": "fk", "child_table": "t_item", "child_columns": ["order_id"],
                "parent_table": "t_order_archive", "parent_columns": ["order_id"],
                "declared": False, "status": "inferred",
                "inclusion": "1.0000", "cardinality_ratio": "0.5000",
                "reason_code": "K_FK_INFERRED", "note": ""}]
    rows = candidates.merge(declared, samples, {})
    assert [row["conflict_type"] for row in rows] == ["mismatch", "mismatch"]
    assert all(row["allowed"] == list(config.REVIEW_DECISIONS) for row in rows)
    declared_row = next(row for row in rows if row["ddl"])
    assert declared_row["alternatives"] == ["t_order_archive.order_id"]
    assert "另见" in declared_row["conflict_detail"]


def test_merge_is_order_independent():
    """同一套依据换输入顺序，候选清单逐字相同（item_id 与 ordinal 都稳定）。"""
    declared = [{"kind": "fk", "child_table": "t_b", "child_columns": ["k"],
                 "parent_table": "t_p", "parent_columns": ["k"],
                 "declared": True, "status": "confirmed", "inclusion": "1.0000",
                 "cardinality_ratio": "1.0000", "reason_code": "K_FK_CONFIRMED",
                 "note": ""},
                {"kind": "fk", "child_table": "t_a", "child_columns": ["p_id"],
                 "parent_table": "t_p", "parent_columns": ["p_id"],
                 "declared": True, "status": "unidirectional", "inclusion": "0.5000",
                 "cardinality_ratio": "1.0000", "reason_code": "K_FK_UNIDIRECTIONAL",
                 "note": ""}]
    forward = candidates.merge(declared, [], {})
    backward = candidates.merge(list(reversed(declared)), [], {})
    assert [row["item_id"] for row in forward] == [row["item_id"] for row in backward]
    assert [row["ordinal"] for row in forward] == [1, 2]
    assert forward[0]["child_table"] == "t_a"


# ---------- 逐条裁定 ----------

def test_decide_persists_and_blocks_publish_until_complete(client_with_run):
    """逐条裁定：一条也要落库；还有未裁项时整版发布被阻断，原因码稳定。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    first = payload["items"][0]["item_id"]
    response = client_with_run.post(
        f"/api/review/{review_id}/decide",
        json={"decisions": [{"item_id": first, "decision": "accept",
                             "note": "两侧一致"}]})
    body = response.json()
    assert body["decisions"]["accept"] == 1
    assert body["decisions"]["pending"] == len(payload["items"]) - 1
    assert body["review"]["status"] == "open"

    blocked = client_with_run.post(f"/api/review/{review_id}/publish", json={})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "R_NOT_READY"

    again = client_with_run.get(f"/api/review/{review_id}").json()
    target = next(item for item in again["items"] if item["item_id"] == first)
    assert target["decision"] == "accept"
    assert target["decision_note"] == "两侧一致"


def test_decide_rejects_unknown_item_and_bad_decision(client_with_run):
    """裁定项与取值都校验：未知 item_id 给 R_UNKNOWN_ITEM，非法取值给 R_BAD_DECISION。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    item_id = payload["items"][0]["item_id"]

    unknown = client_with_run.post(
        f"/api/review/{review_id}/decide",
        json={"decisions": [{"item_id": "rel|nope", "decision": "accept"}]})
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "R_UNKNOWN_ITEM"

    bad = client_with_run.post(
        f"/api/review/{review_id}/decide",
        json={"decisions": [{"item_id": item_id, "decision": "maybe"}]})
    assert bad.status_code == 400
    assert bad.json()["error"] == "R_BAD_DECISION"

    empty = client_with_run.post(f"/api/review/{review_id}/decide",
                                 json={"decisions": []})
    assert empty.status_code == 400


def test_rejected_declaration_stays_in_declared_contract(client_with_run):
    """驳回一条声明关系：建模草稿不含它，但发布版的声明契约区块仍完整保留。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    decisions = []
    for item in payload["items"]:
        decision = "reject" if item["item_id"] == REGION_ID else "accept"
        decisions.append({"item_id": item["item_id"], "decision": decision,
                          "note": "声明缺值，暂不进草稿" if decision == "reject" else ""})
    client_with_run.post(f"/api/review/{review_id}/decide",
                         json={"decisions": decisions})
    published = client_with_run.post(f"/api/review/{review_id}/publish", json={})
    assert published.status_code == 200
    snapshot = published.json()["snapshot"]
    assert snapshot["counts"]["declared_contract"] == X.DECLARED_FK
    assert snapshot["counts"]["draft"] == len(payload["items"]) - 1
    assert snapshot["counts"]["rejected"] == 1
    assert [row["item_id"] for row in snapshot["rejected"]] == [REGION_ID]
    declared_ids = [row["item_id"] for row in snapshot["declared_contract"]]
    assert REGION_ID in declared_ids
    assert PAYMENT_ID not in declared_ids  # 未声明的新关系不在声明契约里
    assert PAYMENT_ID in [row["item_id"] for row in snapshot["draft"]]
    assert snapshot["balanced"] is True


# ---------- 整版发布与重放 ----------

def test_publish_increments_version_and_links_previous(client_with_run):
    """整版发布：版本号从 1 递增，新版本指回上一版，旧版条目不被改写。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    _accept_all(client_with_run, payload)
    first = client_with_run.post(f"/api/review/{review_id}/publish", json={}).json()
    assert first["version_no"] == 1
    assert first["code"] == "R_PUBLISHED"
    assert first["previous_publication_id"] is None
    assert first["snapshot"]["counts"]["draft"] == len(payload["items"])

    target = payload["items"][0]["item_id"]
    client_with_run.post(f"/api/review/{review_id}/decide",
                         json={"decisions": [{"item_id": target,
                                              "decision": "defer"}]})
    second = client_with_run.post(f"/api/review/{review_id}/publish", json={}).json()
    assert second["version_no"] == 2
    assert second["previous_publication_id"] == first["publication_id"]
    assert second["snapshot"]["counts"]["deferred"] == 1

    old = client_with_run.get(f"/api/publications/{first['publication_id']}").json()
    assert old["publication"]["version_no"] == 1
    assert old["snapshot"]["counts"]["deferred"] == 0
    versions = client_with_run.get(f"/api/publications?review_id={review_id}").json()
    assert versions["total"] == 2


def test_replaying_same_package_returns_same_version(client_with_run):
    """裁定包重跑沿用原发布版本：不新增版本、不改当前裁定、逐字同摘要。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    _accept_all(client_with_run, payload)
    first = client_with_run.post(f"/api/review/{review_id}/publish", json={}).json()

    package = client_with_run.get(f"/api/review/{review_id}/package.json").json()
    assert package["basis_digest"] == first["basis_digest"]
    replay = client_with_run.post(f"/api/review/{review_id}/publish",
                                  json=package).json()
    assert replay["replayed"] is True
    assert replay["code"] == "R_REPLAYED"
    assert replay["publication_id"] == first["publication_id"]
    assert replay["version_no"] == first["version_no"]
    assert client_with_run.get(f"/api/publications?review_id={review_id}").json()[
        "total"] == 1

    again = client_with_run.post(f"/api/review/{review_id}/publish", json={}).json()
    assert again["publication_id"] == first["publication_id"]
    assert again["replayed"] is True


def test_stale_package_basis_is_rejected(settings, store, batch_id, review, client):
    """依据漂移后旧裁定包不能套用：basis_digest 不符给 R_STALE_BASIS。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(settings))
    payload = client.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    stale = client.post(f"/api/review/{review_id}/publish",
                        json={"basis_digest": "0" * 64})
    assert stale.status_code == 409
    assert stale.json()["error"] == "R_STALE_BASIS"


def test_failed_publish_keeps_old_version(settings, store, batch_id, monkeypatch):
    """整版发布中途失败：旧版本仍是当前版本，不留半版，裁定包留失败原因。"""
    payload = service.current(store, settings, batch_id)
    review_id = payload["review"]["review_id"]
    service.decide(store, review_id,
                   [{"item_id": item["item_id"], "decision": "accept"}
                    for item in payload["items"]])
    first = service.publish(store, settings, batch_id)
    assert first["version_no"] == 1

    service.decide(store, review_id,
                   [{"item_id": payload["items"][0]["item_id"],
                     "decision": "reject"}])

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写库中断")

    monkeypatch.setattr(store, "save_publication", boom)
    with pytest.raises(errors.Unprocessable) as caught:
        service.publish(store, settings, batch_id)
    assert caught.value.code == "R_PUBLISH_FAILED"
    assert caught.value.status_code == 422

    monkeypatch.undo()
    latest = store.latest_publication(review_id)
    assert latest["publication_id"] == first["publication_id"]
    assert latest["version_no"] == 1
    assert store.list_publications(review_id)[0]["version_no"] == 1
    stored = store.get_review(review_id)
    assert "发布失败" in stored["note"]
    assert stored["status"] == "complete"


def test_resume_reuses_decided_items(store, settings, batch_id):
    """中断后续跑：已裁定项原样沿用，只列未裁项，依据不重算。"""
    payload = service.current(store, settings, batch_id)
    review_id = payload["review"]["review_id"]
    first, second = payload["items"][0]["item_id"], payload["items"][1]["item_id"]
    service.decide(store, review_id,
                   [{"item_id": first, "decision": "accept", "note": "先裁这条"}])
    resumed = service.resume(store, settings, batch_id)
    assert resumed["total"] == len(payload["items"])
    assert resumed["decided"] == 1
    assert resumed["reused"] == 1
    assert resumed["pending_count"] == len(payload["items"]) - 1
    assert first not in [item["item_id"] for item in resumed["pending"]]
    assert second in [item["item_id"] for item in resumed["pending"]]

    again = service.current(store, settings, batch_id)
    kept = next(item for item in again["items"] if item["item_id"] == first)
    assert kept["decision"] == "accept"
    assert kept["decision_note"] == "先裁这条"


def test_review_survives_new_sample_batch_of_same_ddl(settings, tmp_path):
    """换一批样本（同 DDL）：裁定包版本链不重建，旧发布版本仍是当前版本。"""
    sample_dir = tmp_path / "data"
    shutil.copytree(settings.sample_dir, sample_dir)
    swapped = config.Settings(db_path=settings.db_path, sample_dir=str(sample_dir))
    store = Store(swapped.db_path)
    first = batch_service.run(settings=swapped, store=store, source="sample")
    payload = service.current(store, swapped, first["batch_id"])
    review_id = payload["review"]["review_id"]
    service.decide(store, review_id,
                   [{"item_id": item["item_id"], "decision": "accept"}
                    for item in payload["items"]])
    published = service.publish(store, swapped, first["batch_id"])

    _mutate_payment_sample(sample_dir, "SO20261101002", "SO20261101001")
    second = batch_service.run(settings=swapped, store=store, source="sample")
    assert second["batch_id"] != first["batch_id"]
    swapped_review = service.current(store, swapped, second["batch_id"])
    assert swapped_review["review"]["review_id"] == review_id
    assert swapped_review["rebuilt"] is True
    assert swapped_review["latest_publication"]["version_no"] == \
        published["version_no"]
    assert swapped_review["latest_publication"]["publication_id"] == \
        published["publication_id"]
    # 证据变了的条目回到未裁（去重 6 → 5，基数比变了），没变的沿用原裁定
    changed = next(item for item in swapped_review["items"]
                   if item["conflict_type"] == "sample_only")
    assert changed["sample"]["cardinality_ratio"] == "0.6250"
    assert changed["decision"] == "pending"
    kept = next(item for item in swapped_review["items"]
                if item["item_id"] == candidates.relation_id(
                    "odl_sale_order", ["member_id"], "odl_member", ["member_id"]))
    assert kept["decision"] == "accept"


def test_review_page_and_exports(client_with_run):
    """裁定页可打开；发布版导出同时含声明契约与建模草稿两个区块。"""
    page = client_with_run.get("/review")
    assert page.status_code == 200
    assert "关系候选" in page.text
    assert "契约裁定" in page.text

    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    _accept_all(client_with_run, payload)
    published = client_with_run.post(f"/api/review/{review_id}/publish",
                                     json={}).json()
    export = client_with_run.get(
        f"/api/publications/{published['publication_id']}/export.csv")
    assert export.status_code == 200
    assert export.content.startswith(b"\xef\xbb\xbf")
    text = export.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header == "区块,子表,子列,父表,父列,冲突形态,裁定,声明依据,样本依据,裁定注记"
    assert "声明契约" in text and "建模草稿" in text
    assert "odl_store,region_code,odl_region,region_code" in text
    assert "odl_payment,order_id,odl_sale_order,order_id" in text


def test_review_empty_state_before_first_run(client):
    """没有批次时裁定页给空态、接口给 404 与 B_EMPTY。"""
    page = client.get("/review")
    assert page.status_code == 200
    assert "还没有批次。" in page.text
    missing = client.get("/api/review")
    assert missing.status_code == 404
    assert missing.json()["error"] == "B_EMPTY"


def test_spec_shows_published_contract_and_draft(client_with_run):
    """规格单只读当前发布版本：声明契约与建模草稿并排，键结论标出发布归属。"""
    before = client_with_run.get("/api/spec").json()
    assert before["contract"]["publication"] is None
    assert before["contract"]["declared_contract"] == []

    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    decisions = []
    for item in payload["items"]:
        decision = "reject" if item["item_id"] == REGION_ID else "accept"
        decisions.append({"item_id": item["item_id"], "decision": decision,
                          "note": "声明缺值" if decision == "reject" else ""})
    client_with_run.post(f"/api/review/{review_id}/decide",
                         json={"decisions": decisions})
    client_with_run.post(f"/api/review/{review_id}/publish", json={})

    spec = client_with_run.get("/api/spec").json()
    contract = spec["contract"]
    assert contract["publication"]["version_no"] == 1
    assert len(contract["declared_contract"]) == X.DECLARED_FK
    assert len(contract["draft"]) == len(payload["items"]) - 1
    assert len(contract["rejected"]) == 1
    assert contract["pending"] == 0

    labels = {(row["child_table"], row["parent_table"]): row["contract"]
              for row in spec["keys"] if row["kind"] == "fk"}
    assert labels[("odl_store", "odl_region")] == "声明契约·驳回"
    assert labels[("odl_payment", "odl_sale_order")] == "建模草稿"
    assert labels[("odl_sale_order", "odl_store")] == "建模草稿"

    page = client_with_run.get("/spec")
    assert page.status_code == 200
    assert "声明契约" in page.text and "建模草稿" in page.text
    assert "已发布版本 v1" in page.text


def test_spec_contract_ignores_other_ddl_lineage(store, settings, batch_id):
    """规格单只取同 DDL 的裁定包；别的 DDL 发布了版本也不串到这张单上。"""
    payload = service.current(store, settings, batch_id)
    assert payload["review"]["review_id"] == service.review_id_for(
        payload["declaration_digest"], settings.rule_version)
    other = service.review_id_for("0" * 64, settings.rule_version)
    assert other != payload["review"]["review_id"]
    assert store.get_review(other) is None


def test_review_status_tracks_publication_digest(client_with_run):
    """裁定包状态：未裁完 open、裁完未发 complete、与最新版同摘要 published。"""
    payload = client_with_run.get("/api/review").json()
    review_id = payload["review"]["review_id"]
    assert payload["review"]["status"] == "open"
    assert payload["published"] is False

    _accept_all(client_with_run, payload)
    complete = client_with_run.get(f"/api/review/{review_id}").json()
    assert complete["review"]["status"] == "complete"
    assert complete["published"] is False

    client_with_run.post(f"/api/review/{review_id}/publish", json={})
    published = client_with_run.get(f"/api/review/{review_id}").json()
    assert published["review"]["status"] == "published"
    assert published["published"] is True

    target = payload["items"][0]["item_id"]
    client_with_run.post(f"/api/review/{review_id}/decide",
                         json={"decisions": [{"item_id": target,
                                              "decision": "defer"}]})
    edited = client_with_run.get(f"/api/review/{review_id}").json()
    assert edited["review"]["status"] == "complete"
    assert edited["published"] is False
    assert edited["latest_publication"]["version_no"] == 1


def test_conflicting_duplicate_package_returns_existing_version(settings, store,
                                                                batch_id, monkeypatch):
    """并发重放：写库撞唯一键时按已有版本返回，不新增版本也不报失败。"""
    payload = service.current(store, settings, batch_id)
    review_id = payload["review"]["review_id"]
    service.decide(store, review_id,
                   [{"item_id": item["item_id"], "decision": "accept"}
                    for item in payload["items"]])
    first = service.publish(store, settings, batch_id)

    real_save = store.save_publication

    def racing_save(publication, items, review_update=None):
        # 模拟另一请求刚写完同一 package_digest
        real_save(publication, items, review_update)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: publications")

    monkeypatch.setattr(store, "save_publication", racing_save)
    # 先清掉内存中的同摘要发布，逼它重新走写入路径
    with store.connect() as conn:
        conn.execute("DELETE FROM publication_items WHERE publication_id=?",
                     (first["publication_id"],))
        conn.execute("DELETE FROM publications WHERE publication_id=?",
                     (first["publication_id"],))
    replayed = service.publish(store, settings, batch_id)
    assert replayed["replayed"] is True
    assert replayed["package_digest"] == first["package_digest"]
    assert service.publications(store, review_id)["total"] == 1


def test_publishing_from_old_batch_uses_latest_evidence(settings, tmp_path):
    """从旧批次号发布会倒回旧样本：取证批次一律取同 DDL 下最新一次接入。"""
    sample_dir = tmp_path / "data"
    shutil.copytree(settings.sample_dir, sample_dir)
    swapped = config.Settings(db_path=settings.db_path, sample_dir=str(sample_dir))
    store = Store(swapped.db_path)
    first = batch_service.run(settings=swapped, store=store, source="sample")
    _mutate_payment_sample(sample_dir, "SO20261101002", "SO20261101001")
    second = batch_service.run(settings=swapped, store=store, source="sample")
    assert second["batch_id"] != first["batch_id"]
    grounded = service.grounded_batch(store, first["batch_id"])
    assert grounded == second["batch_id"]

    payload = service.current(store, swapped, first["batch_id"])
    assert payload["batch_id"] == second["batch_id"]
    assert payload["requested_batch_id"] == first["batch_id"]
    payment = next(item for item in payload["items"]
                   if item["conflict_type"] == "sample_only")
    assert payment["sample"]["cardinality_ratio"] == "0.6250"
