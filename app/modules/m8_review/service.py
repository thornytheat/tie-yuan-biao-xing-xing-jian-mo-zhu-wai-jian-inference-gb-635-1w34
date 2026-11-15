"""M8 编排：两套依据 → 冲突标记 → 逐条裁定 → 整版发布。

关键约束（与接入/剖析流水线解耦，不进五段阶段，不写批次行）：
1. 依据只读：声明依据取自本批次已落库的 M4 结论，样本依据按同名列与包含度重算；
   两侧各自快照，互不覆盖。驳回一条声明项只把它挡在建模草稿外，
   已发布规格的「声明契约」区块仍保留原声明。
2. 裁定持久：每裁定一条就落库；进程中断后未裁的仍是 pending，已裁的原样保留。
3. 整版发布：一个事务写版本头、全部条目与裁定包状态；失败整体回滚，
   上一版仍是当前版本。
4. 重放幂等：同一依据 + 同一裁定包算出同一 package_digest，直接返回原发布版本，
   不新增版本、不重算结论。依据漂移（样本变了）时旧裁定包不套用，报 R_STALE_BASIS。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

from ... import config, errors, manifest, reasons
from ..m1_ingest import service as m1_service
from ..m2_contract import service as m2_service
from ..m5_batches import service as batch_service
from . import candidates

DECISION_LABELS = config.REVIEW_DECISION_LABELS


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class _Modeled:
    """把落库的键结论包成 candidates 期望的形状。"""

    def __init__(self, keys: list):
        self.keys = keys


def basis_of(bundle: dict, coerced, modeled) -> dict:
    """依据：声明侧来自落库结论，样本侧重算；两侧都只含数值与原因码。"""
    items = candidates.candidates(coerced, modeled)
    manifest_doc = bundle.get("manifest") or {}
    ddl_digest = ""
    for item in manifest_doc.get("inputs") or []:
        if item.get("role") == "ddl":
            ddl_digest = item.get("sha256", "")
            break
    return {
        "items": items,
        "declaration_digest": ddl_digest,
        "input_digest": manifest_doc.get("input_digest", ""),
        "rule_version": manifest_doc.get("rule_version", config.RULE_VERSION),
        "batch_id": bundle["batch"]["batch_id"],
    }


def basis_digest(basis: dict) -> str:
    return _digest({
        "batch_id": basis["batch_id"],
        "declaration": basis["declaration_digest"],
        "input": basis["input_digest"],
        "rule_version": basis["rule_version"],
        "items": candidates.signature(basis["items"]),
    })


def package_digest(basis_hash: str, decisions: dict[str, dict]) -> str:
    """裁定包摘要：依据 + 按 item_id 排序的裁定与注记，与提交顺序无关。"""
    ordered = [{"item_id": item_id,
                "decision": decisions[item_id]["decision"],
                "note": decisions[item_id].get("note", "")}
               for item_id in sorted(decisions)]
    return _digest({"basis": basis_hash, "decisions": ordered})


def context(settings, store, batch_id: str) -> dict:
    bundle = batch_service.bundle(store, batch_id)
    coerced = m2_service.build(m1_service.intake(settings), settings)
    modeled = _Modeled(bundle["keys"])
    return {"bundle": bundle, "coerced": coerced, "modeled": modeled,
            "basis": basis_of(bundle, coerced, modeled)}


def _manifest_of(store, batch_id: str) -> dict:
    return store.load_manifest(batch_id) or {}


def _declaration_of(store, batch_id: str) -> str:
    for item in _manifest_of(store, batch_id).get("inputs") or []:
        if item.get("role") == "ddl":
            return item.get("sha256", "")
    return ""


def _rule_version_of(store, batch_id: str) -> str:
    return _manifest_of(store, batch_id).get("rule_version", config.RULE_VERSION)


def grounded_batch(store, batch_id: str, *, limit: int = 50) -> str:
    """取证批次：同一 DDL 契约下最新的那次接入。

    裁定包按 DDL 契约归属，依据必须跟最新的样本走；否则从旧批次号发布会把
    依据倒退回旧样本，把已裁的结论套到过期证据上。
    """
    declaration = _declaration_of(store, batch_id)
    if not declaration:
        return batch_id
    for batch in store.list_batches(limit):
        if batch["batch_id"] == batch_id:
            break
        if _declaration_of(store, batch["batch_id"]) == declaration:
            return batch["batch_id"]
    return batch_id


def review_id_for(declaration_digest: str, rule_version: str) -> str:
    """裁定归属声明契约：同一份 DDL + 同一规则版本共用一个裁定包版本链。

    这样样本换了一批（已发布规格的旧依据仍在）时，裁定包不会凭空多出一个，
    旧发布版本也还有「上一版」可对照；样本变化只刷新候选的依据快照。
    """
    return "RV-" + hashlib.sha256(
        f"{declaration_digest}\x00{rule_version}".encode("utf-8")).hexdigest()[:16]


def ensure_review(store, settings, batch_id: str) -> dict:
    """按当前依据建/刷新裁定包。

    - 依据未变：沿用原裁定（中断续跑与重跑都走这条路）；
    - 依据变了（换样本）：刷新候选与两侧依据快照；两侧依据逐字未变的条目按
      item_id 沿用原裁定，证据变了的回到未裁；当前发布版本不受影响。
    取证批次一律取同 DDL 契约下最新的接入，不因传入旧批次号而倒回旧样本。
    """
    batch_id = grounded_batch(store, batch_id)
    parts = context(settings, store, batch_id)

    basis = parts["basis"]
    for item in basis["items"]:
        item["signature"] = candidates.item_signature(item)
    digest = basis_digest(basis)
    review_id = review_id_for(basis["declaration_digest"], basis["rule_version"])
    existing = store.get_review(review_id)
    now = _now()

    if existing is not None and existing["basis_digest"] == digest:
        store.replace_review_items(review_id, basis["items"],
                                   preserve_decisions=True)
        return {"review": store.get_review(review_id), "basis": basis,
                "rebuilt": False}

    previous = store.latest_publication(review_id) if existing else None
    store.save_review({
        "review_id": review_id,
        "batch_id": batch_id,
        "declaration_digest": basis["declaration_digest"],
        "rule_version": basis["rule_version"],
        "basis_digest": digest,
        "previous_publication_id": (previous or {}).get("publication_id"),
        "status": "open",
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "published_publication_id": None,
        "note": "依据变化，已按 item_id 沿用原裁定并重开" if existing else "",
    })
    store.replace_review_items(review_id, basis["items"],
                               preserve_decisions=True)
    items = _items(store, review_id)
    store.touch_review(review_id, status=_status_of(store, store.get_review(review_id),
                                                    items), updated_at=now)
    return {"review": store.get_review(review_id), "basis": basis,
            "rebuilt": existing is not None}


def _items(store, review_id: str) -> list[dict]:
    return store.load_review_items(review_id)


def view(store, review: dict, items: list[dict] | None = None) -> dict:
    items = _items(store, review["review_id"]) if items is None else items
    conflicts = candidates.conflict_counts(items)
    decisions = candidates.decision_counts(items)
    latest = store.latest_publication(review["review_id"])
    current_package = package_digest(
        review["basis_digest"],
        {item["item_id"]: {"decision": item.get("decision") or "pending",
                           "note": item.get("decision_note", "")}
         for item in items})
    return {
        "review": review,
        "items": [_item_view(item) for item in items],
        # 当前裁定是否已有同摘要的发布版本：决定页面是「已发布」还是「有改动未发」
        "published": bool(latest and latest["package_digest"] == current_package),
        "conflicts": conflicts,
        "decisions": decisions,
        "pending_ids": [item["item_id"] for item in items
                        if (item.get("decision") or "pending") == "pending"],
        "ready": bool(items) and decisions["pending"] == 0,
        "latest_publication": _publication_head(latest) if latest else None,
        "labels": {
            "decision": DECISION_LABELS,
            "conflict": config.REVIEW_CONFLICT_LABELS,
            "status": config.REVIEW_STATUS_LABELS,
        },
        "notes": _notes(conflicts, decisions, latest),
    }


def _item_view(item: dict) -> dict:
    decision = item.get("decision") or "pending"
    return {
        "item_id": item["item_id"],
        "ordinal": item["ordinal"],
        "kind": item["kind"],
        "child_table": item["child_table"],
        "child_columns": list(item["child_columns"]),
        "parent_table": item["parent_table"],
        "parent_columns": list(item["parent_columns"]),
        "conflict_type": item["conflict_type"],
        "conflict_label": config.REVIEW_CONFLICT_LABELS.get(item["conflict_type"],
                                                          item["conflict_type"]),
        "conflict_detail": item["conflict_detail"],
        "alternatives": list(item["alternatives"]),
        "ddl": item["ddl"],
        "sample": item["sample"],
        "declared_sample": item["declared_sample"],
        "basis_note": candidates._basis_note(item),
        "decision": decision,
        "decision_label": DECISION_LABELS.get(decision, decision),
        "decision_note": item.get("decision_note", ""),
        "decided_at": item.get("decided_at"),
        "reason_code": candidates._reason_of(item["conflict_type"]),
        "allowed": list(item.get("allowed") or config.REVIEW_DECISIONS),
    }


def _publication_head(publication: dict) -> dict:
    snapshot = json.loads(publication.get("snapshot_json") or "{}")
    return {
        "publication_id": publication["publication_id"],
        "version_no": publication["version_no"],
        "package_digest": publication["package_digest"],
        "basis_digest": publication["basis_digest"],
        "previous_publication_id": publication.get("previous_publication_id"),
        "created_at": publication["created_at"],
        "counts": snapshot.get("counts") or {},
        "conflicts": snapshot.get("conflicts") or {},
        "balanced": bool(snapshot.get("balanced")),
    }


def _notes(conflicts: dict, decisions: dict, latest: dict | None) -> list:
    return [
        f"关系候选 {sum(conflicts.values())} 条："
        f"同列异父 {conflicts.get('mismatch', 0)}、"
        f"声明独有 {conflicts.get('declared_only', 0)}、"
        f"样本独有 {conflicts.get('sample_only', 0)}、"
        f"两侧一致 {conflicts.get('agreement', 0)}",
        f"裁定：采纳 {decisions.get('accept', 0)}、驳回 {decisions.get('reject', 0)}、"
        f"挂起 {decisions.get('defer', 0)}、未裁 {decisions.get('pending', 0)}",
        (f"当前版本 v{latest['version_no']}（{latest['publication_id']}）"
         if latest else "还没有发布版本"),
    ]


# ---------- 逐条裁定 ----------

def _validate_decisions(items: dict, entries) -> list[dict]:
    if not isinstance(entries, list) or not entries:
        raise errors.BadRequest("decisions 必须是非空数组", code="R_BAD_DECISION")
    cleaned = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise errors.BadRequest(f"第 {index} 条裁定不是对象", code="R_BAD_DECISION")
        item_id = str(entry.get("item_id") or "").strip()
        decision = str(entry.get("decision") or "").strip()
        if item_id not in items:
            raise errors.BadRequest(
                f"第 {index} 条裁定项 {item_id or '空'} 不在本裁定包",
                code="R_UNKNOWN_ITEM")
        if decision not in config.REVIEW_DECISIONS:
            raise errors.BadRequest(
                f"第 {index} 条裁定 {decision or '空'} 只能是 "
                f"{list(config.REVIEW_DECISIONS)}", code="R_BAD_DECISION")
        if decision not in (items[item_id].get("allowed")
                            or config.REVIEW_DECISIONS):
            raise errors.BadRequest(
                f"第 {index} 条裁定项不允许 {decision}", code="R_BAD_DECISION")
        cleaned.append({"item_id": item_id, "decision": decision,
                        "note": str(entry.get("note") or "").strip()})
    return cleaned


def decide(store, review_id: str, decisions: list[dict]) -> dict:
    """一条或多条裁定落库；已裁项可改写，每次都留时间与注记。"""
    review = store.get_review(review_id)
    if review is None:
        raise errors.NotFound(f"裁定包不存在：{review_id}", code="R_UNKNOWN_ITEM")
    items = {item["item_id"]: item for item in _items(store, review_id)}
    if not items:
        raise errors.Conflict("本轮没有关系候选，无法裁定", code="R_NO_BASIS")
    cleaned = _validate_decisions(items, decisions)
    now = _now()
    for entry in cleaned:
        store.decide_review_item(review_id, entry["item_id"],
                                 decision=entry["decision"],
                                 note=entry["note"], decided_at=now)
    refreshed = _items(store, review_id)
    status = _status_of(store, store.get_review(review_id), refreshed)
    store.touch_review(review_id, status=status, updated_at=now)
    return view(store, store.get_review(review_id), refreshed)


def _status_of(store, review: dict, items: list[dict]) -> str:
    """裁定包状态：未裁完 open、已裁完 complete、与最新发布版本同摘要 published。"""
    counts = candidates.decision_counts(items)
    if counts["pending"]:
        return "open"
    latest = store.latest_publication(review["review_id"])
    if latest is None:
        return "complete"
    current = package_digest(
        review["basis_digest"],
        {item["item_id"]: {"decision": item.get("decision") or "pending",
                           "note": item.get("decision_note", "")}
         for item in items})
    return "published" if latest["package_digest"] == current else "complete"


# ---------- 整版发布 ----------

def _relation_row(item: dict) -> dict:
    return {
        "item_id": item["item_id"],
        "child_table": item["child_table"],
        "child_columns": list(item["child_columns"]),
        "parent_table": item["parent_table"],
        "parent_columns": list(item["parent_columns"]),
        "conflict_type": item["conflict_type"],
        "decision": item.get("decision") or "pending",
        "decision_note": item.get("decision_note", ""),
        "basis": {"ddl": item.get("ddl"), "sample": item.get("sample"),
                  "declared_sample": item.get("declared_sample")},
    }


def build_snapshot(review: dict, items: list[dict], version_no: int,
                   package_hash: str, previous_id: str | None) -> dict:
    """发布版本快照：声明契约永久保留，建模草稿只收采纳项，驳回与挂起单列。"""
    declared = [item for item in items if item.get("ddl")]
    accepted = [item for item in items if (item.get("decision") or "") == "accept"]
    rejected = [item for item in items if (item.get("decision") or "") == "reject"]
    deferred = [item for item in items if (item.get("decision") or "") == "defer"]
    pending = [item for item in items
               if (item.get("decision") or "pending") == "pending"]
    return {
        "review_id": review["review_id"],
        "batch_id": review["batch_id"],
        "version_no": version_no,
        "package_digest": package_hash,
        "basis_digest": review["basis_digest"],
        "previous_publication_id": previous_id,
        # 声明契约：所有 DDL 声明关系，无论裁定结果都在这里留档
        "declared_contract": [_relation_row(item) for item in declared],
        # 建模草稿：只收裁定为采纳的关系（含样本证实的新关系）
        "draft": [_relation_row(item) for item in accepted],
        "rejected": [_relation_row(item) for item in rejected],
        "deferred": [_relation_row(item) for item in deferred],
        "pending": [_relation_row(item) for item in pending],
        "conflicts": candidates.conflict_counts(items),
        "counts": {
            "items": len(items),
            "declared_contract": len(declared),
            "draft": len(accepted),
            "rejected": len(rejected),
            "deferred": len(deferred),
            "pending": len(pending),
        },
        # 账：每条候选恰好落在采纳 / 驳回 / 挂起之一；声明项必须全部进声明契约
        "balanced": (len(items) == len(accepted) + len(rejected) + len(deferred)
                     and len(declared) <= len(items)
                     and not pending),
    }


def publish(store, settings, batch_id: str, package: dict | None = None) -> dict:
    """整版发布：先确认依据未漂移，再按裁定包算摘要；同摘要直接复用原版本。"""
    package = package if isinstance(package, dict) else {}
    ensured = ensure_review(store, settings, batch_id)
    batch_id = ensured["review"]["batch_id"]
    review = ensured["review"]
    current_digest = review["basis_digest"]
    claimed = str(package.get("basis_digest") or "").strip()
    if claimed and claimed != current_digest:
        raise errors.Conflict(
            "裁定包依据与当前样本不一致，旧裁定不能套在新样本上",
            code="R_STALE_BASIS",
            detail={"claimed": claimed, "current": current_digest})

    items = _items(store, review["review_id"])
    if not items:
        raise errors.Conflict("本轮没有关系候选，无法发布", code="R_NO_BASIS")
    incoming = package.get("decisions")
    current = {item["item_id"]: {"decision": item.get("decision") or "pending",
                                 "note": item.get("decision_note", "")}
               for item in items}

    if incoming:
        # 先按「提交的裁定包」算摘要：命中已有版本就原样复用，不改当前裁定状态。
        submitted = {
            item["item_id"]: {"decision": item.get("decision") or "pending",
                              "note": item.get("decision_note", "")}
            for item in items
        }
        for entry in _validate_decisions({item["item_id"]: item for item in items},
                                         incoming):
            submitted[entry["item_id"]] = {"decision": entry["decision"],
                                          "note": entry["note"]}
        submitted_hash = package_digest(current_digest, submitted)
        existing = store.find_publication_by_package(review["review_id"],
                                                     submitted_hash)
        if existing is not None:
            return _publication_payload(existing, replayed=True)
        # 没有命中才落裁定；改写只影响下一个版本，已发布版本逐字不可变
        decide(store, review["review_id"], incoming)
        review = store.get_review(review["review_id"])
        items = _items(store, review["review_id"])
        current = {item["item_id"]: {"decision": item.get("decision") or "pending",
                                     "note": item.get("decision_note", "")}
                   for item in items}

    pending = sorted(item_id for item_id, entry in current.items()
                     if entry["decision"] == "pending")
    if pending:
        raise errors.Conflict(
            f"还有 {len(pending)} 条未裁定，整版发布前须逐条裁定："
            f"{'、'.join(pending[:3])}", code="R_NOT_READY",
            detail={"pending": pending})
    package_hash = package_digest(current_digest, current)
    existing = store.find_publication_by_package(review["review_id"], package_hash)
    if existing is not None:
        store.touch_review(review["review_id"], status="published",
                           updated_at=_now(),
                           published_publication_id=existing["publication_id"])
        return _publication_payload(existing, replayed=True)

    previous = store.latest_publication(review["review_id"])
    version_no = (previous["version_no"] if previous else 0) + 1
    publication_id = manifest.new_run_id("PUB")
    snapshot = build_snapshot(review, items, version_no, package_hash,
                              previous["publication_id"] if previous else None)
    publication = {
        "publication_id": publication_id,
        "review_id": review["review_id"],
        "version_no": version_no,
        "package_digest": package_hash,
        "basis_digest": current_digest,
        "previous_publication_id": previous["publication_id"] if previous else None,
        "status": "published",
        "created_at": _now(),
        "snapshot": snapshot,
        "note": reasons.label("R_PUBLISHED"),
    }
    rows = [_publication_item(item) for item in items]
    try:
        store.save_publication(publication, rows, review_update={
            "review_id": review["review_id"], "status": "published",
            "updated_at": publication["created_at"],
            "published_publication_id": publication_id,
            "note": ""})
    except sqlite3.IntegrityError:
        # 并发重放：同一裁定包已被另一请求发布，按重放返回那个版本，不新增
        duplicate = store.find_publication_by_package(review["review_id"],
                                                      package_hash)
        if duplicate is not None:
            store.touch_review(review["review_id"], status="published",
                               updated_at=_now(),
                               published_publication_id=duplicate["publication_id"])
            return _publication_payload(duplicate, replayed=True)
        raise
    except Exception as exc:  # noqa: BLE001 - 发布失败要留痕并保住旧版
        store.touch_review(review["review_id"], status="complete",
                           updated_at=_now(),
                           note=f"发布失败：{type(exc).__name__}: {exc}")
        raise errors.Unprocessable(
            f"整版发布失败，旧版本保持不变：{type(exc).__name__}: {exc}",
            code="R_PUBLISH_FAILED",
            detail={"publication_id": publication_id}) from exc
    return _publication_payload(store.get_publication(publication_id),
                                replayed=False)


def _publication_item(item: dict) -> dict:
    decision = item.get("decision") or "pending"
    if decision == "accept":
        status = "draft"
    elif item.get("ddl"):
        status = "declared_contract"
    else:
        status = "none"
    return {
        "item_id": item["item_id"], "ordinal": item["ordinal"], "kind": item["kind"],
        "child_table": item["child_table"], "child_columns": item["child_columns"],
        "parent_table": item["parent_table"], "parent_columns": item["parent_columns"],
        "decision": decision, "conflict_type": item["conflict_type"],
        "basis": {"ddl": item.get("ddl"), "sample": item.get("sample"),
                  "declared_sample": item.get("declared_sample")},
        "published": decision == "accept",
        "published_status": status,
    }


def _publication_payload(publication: dict, *, replayed: bool) -> dict:
    snapshot = json.loads(publication["snapshot_json"])
    return {
        "publication_id": publication["publication_id"],
        "review_id": publication["review_id"],
        "version_no": publication["version_no"],
        "package_digest": publication["package_digest"],
        "basis_digest": publication["basis_digest"],
        "previous_publication_id": publication["previous_publication_id"],
        "created_at": publication["created_at"],
        "replayed": replayed,
        "code": "R_REPLAYED" if replayed else "R_PUBLISHED",
        "message": reasons.label("R_REPLAYED" if replayed else "R_PUBLISHED"),
        "snapshot": snapshot,
    }


def publications(store, review_id: str | None = None) -> dict:
    rows = [_publication_head(item) for item in store.list_publications(review_id)]
    return {"rows": rows, "total": len(rows)}


def publication_detail(store, publication_id: str) -> dict:
    publication = store.get_publication(publication_id)
    if publication is None:
        raise errors.NotFound(f"发布版本不存在：{publication_id}", code="B_EMPTY")
    return {
        "publication": _publication_head(publication),
        "snapshot": json.loads(publication["snapshot_json"]),
        "items": store.load_publication_items(publication_id),
    }


def current(store, settings, batch_id: str | None = None) -> dict:
    """当前裁定包视图；没有批次时给出稳定空态。"""
    if batch_id is None:
        batch = store.latest_batch()
        if batch is None:
            raise errors.NotFound("还没有任何批次，请先跑一次接入", code="B_EMPTY")
        batch_id = batch["batch_id"]
    ensured = ensure_review(store, settings, batch_id)
    payload = view(store, ensured["review"])
    payload["batch_id"] = ensured["review"]["batch_id"]
    payload["requested_batch_id"] = batch_id
    payload["rebuilt"] = ensured["rebuilt"]
    payload["declaration_digest"] = ensured["review"]["declaration_digest"]
    return payload


def resume(store, settings, batch_id: str) -> dict:
    """中断后续跑：只补未裁项，已裁项按原裁定沿用，不重算依据。"""
    ensured = ensure_review(store, settings, batch_id)
    review = ensured["review"]
    items = _items(store, review["review_id"])
    pending = [item for item in items
               if (item.get("decision") or "pending") == "pending"]
    decided = len(items) - len(pending)
    latest = store.latest_publication(review["review_id"])
    return {
        "review_id": review["review_id"],
        "batch_id": batch_id,
        "status": review["status"],
        "basis_digest": review["basis_digest"],
        "reopened": ensured["rebuilt"],
        "total": len(items),
        "decided": decided,
        "reused": decided,
        "pending": [_item_view(item) for item in pending],
        "pending_count": len(pending),
        "latest_publication": _publication_head(latest) if latest else None,
        "note": (f"沿用已裁定 {decided} 条，待裁定 {len(pending)} 条"
                 if pending else f"已裁定 {decided} 条，可整版发布"),
    }
