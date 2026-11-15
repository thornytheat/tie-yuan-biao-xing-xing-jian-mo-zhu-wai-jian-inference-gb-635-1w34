"""M6 编排：把落库结果装配成规格单，附带脚注与导出载荷。

规格单同时给出两套依据：键结论（含声明外键与推断外键）与已发布契约区块
（声明契约 + 建模草稿 + 驳回/挂起 + 待裁定数），评审不用在页面间对照。
"""
from __future__ import annotations

import json

from ... import config
from ..m4_keys import evidence
from ..m5_batches import service as batch_service
from ..m8_review import candidates as m8_candidates
from ..m8_review import service as m8_service
from . import csv_out, json_out, spec_out


def build(store, batch_id: str) -> dict:
    bundle = batch_service.bundle(store, batch_id)
    notes = evidence.footnotes(bundle["keys"], bundle["star"],
                               bundle["accounts"] or {})
    document = spec_out.document(bundle, notes)
    document["evidence_rows"] = [
        {"table_name": row.get("table_name"), "column_name": row.get("column_name"),
         "row_ref": row.get("row_ref"), "raw_value": row.get("raw_value"),
         "code": row.get("code"), "detail": row.get("detail")}
        for row in bundle["quarantine"]
    ]
    document["contract"] = contract_block(store, bundle)
    _annotate_keys(document)
    if document["contract"]["publication"]:
        publication = document["contract"]["publication"]
        document["footnotes"] = list(notes) + [
            f"已发布契约 v{publication['version_no']}：声明 {publication['counts'].get('declared_contract', 0)} 条，"
            f"建模草稿 {publication['counts'].get('draft', 0)} 条",
            f"仍待裁定 {document['contract']['pending']} 条，未进任何发布版本",
        ]
    return document


def _annotate_keys(document: dict) -> None:
    """给键结论补一列「已发布裁定」：评审能一眼看出这条是否进了草稿。"""
    contract = document.get("contract") or {}
    status = {}
    for block in ("declared_contract", "draft", "rejected", "deferred"):
        for row in contract.get(block) or []:
            prior = status.get(row["item_id"])
            if prior is None or block == "draft":
                status[row["item_id"]] = (block, row.get("decision") or "")
    for row in document.get("key_rows") or []:
        if row["kind"] != "fk":
            row["contract"] = "—"
            continue
        item_id = m8_candidates.relation_id(
            row["child_table"], row["child_columns"], row["parent_table"],
            row.get("parent_columns") or [])
        block, decision = status.get(item_id, ("", ""))
        label = {"declared_contract": "声明契约", "draft": "建模草稿",
                 "rejected": "驳回", "deferred": "挂起"}.get(block, "")
        if not label:
            row["contract"] = "未进发布" if contract.get("publication") else "未发布"
            continue
        row["contract"] = (f"{label}·{config.REVIEW_DECISION_LABELS.get(decision, decision)}"
                           if block == "declared_contract" else label)


def contract_block(store, bundle: dict) -> dict:
    """规格单的契约区块：取同名 DDL 裁定包的当前发布版本，不新开裁定。"""
    manifest_doc = bundle.get("manifest") or {}
    declaration = ""
    for item in manifest_doc.get("inputs") or []:
        if item.get("role") == "ddl":
            declaration = item.get("sha256", "")
            break
    empty = {"publication": None, "declared_contract": [], "draft": [],
             "rejected": [], "deferred": [], "pending": 0, "review_id": "",
             "conflicts": {}}
    if not declaration:
        return empty
    review_id = m8_service.review_id_for(declaration,
                                         manifest_doc.get("rule_version", ""))
    review = store.get_review(review_id)
    if review is None:
        return empty
    publication = store.latest_publication(review_id)
    items = store.load_review_items(review_id)
    pending = len([item for item in items
                   if (item.get("decision") or "pending") == "pending"])
    if publication is None:
        return {**empty, "review_id": review_id, "pending": pending,
                "conflicts": m8_candidates.conflict_counts(items)}
    snapshot = json.loads(publication["snapshot_json"])
    return {
        "review_id": review_id,
        "publication": {
            "publication_id": publication["publication_id"],
            "version_no": publication["version_no"],
            "package_digest": publication["package_digest"],
            "basis_digest": publication["basis_digest"],
            "created_at": publication["created_at"],
            "counts": snapshot.get("counts") or {},
            "conflicts": snapshot.get("conflicts") or {},
            "balanced": bool(snapshot.get("balanced")),
        },
        "declared_contract": snapshot.get("declared_contract") or [],
        "draft": snapshot.get("draft") or [],
        "rejected": snapshot.get("rejected") or [],
        "deferred": snapshot.get("deferred") or [],
        "pending": pending,
        "conflicts": snapshot.get("conflicts") or {},
    }


def to_payload(document: dict) -> dict:
    """接口返回：去掉模板专用字段，只留数据。"""
    fields = ["title", "accounts", "account_rows", "columns", "objects",
              "facts", "dims", "pending", "footnotes", "total_rows", "tables",
              "contract"]
    payload = {field: document.get(field) for field in fields}
    payload["keys"] = document.get("key_rows")
    payload["batch_id"] = document["batch"]["batch_id"]
    payload["run_id"] = document["batch"]["run_id"]
    payload["input_digest"] = (document.get("manifest") or {}).get("input_digest", "")
    return payload


def columns_csv(document: dict) -> bytes:
    return csv_out.encode(csv_out.columns_csv(document["columns"]))


def evidence_csv(document: dict) -> bytes:
    return csv_out.encode(csv_out.evidence_csv(document["evidence_rows"]))


def manifest_json(document: dict) -> bytes:
    payload = {
        "run_id": document["batch"]["run_id"],
        "batch_id": document["batch"]["batch_id"],
        "batch_key": document["batch"]["batch_key"],
        "input_digest": (document.get("manifest") or {}).get("input_digest", ""),
        "inputs": (document.get("manifest") or {}).get("inputs", []),
        "params": (document.get("manifest") or {}).get("params", {}),
        "counters": (document.get("manifest") or {}).get("counters", {}),
        "accounts": document.get("accounts"),
    }
    return json_out.encode(json_out.dumps(payload, indent=2))


def accounts_payload(raw: str) -> dict:
    return json.loads(raw)
