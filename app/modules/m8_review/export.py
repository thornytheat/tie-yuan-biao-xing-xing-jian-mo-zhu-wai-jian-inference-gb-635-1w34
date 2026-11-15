"""M8 导出：已发布版本的声明契约与建模草稿导出，以及可重放的裁定包。

导出必须同时含两个区块，否则评审只能看到建模草稿，误以为声明关系被删掉了：

- `declared_contract`：全部 DDL 声明关系（含被驳回、挂起、样本不支撑的）；
- `draft`：裁定为采纳的关系，其中既可能是声明关系，也可能是样本证实的新关系。
"""
from __future__ import annotations

import csv
import io

SNAPSHOT_HEADERS = ("区块", "子表", "子列", "父表", "父列", "冲突形态", "裁定",
                    "声明依据", "样本依据", "裁定注记")
BLOCK_LABELS = {
    "declared_contract": "声明契约",
    "draft": "建模草稿",
    "rejected": "驳回",
    "deferred": "挂起",
}


def _side_text(side: dict | None) -> str:
    if not side:
        return ""
    return (f"{side.get('source', '')}:{side.get('status', '')}"
            f" 包含度={side.get('inclusion') or '—'}"
            f" 基数比={side.get('cardinality_ratio') or '—'}")


def snapshot_rows(snapshot: dict) -> list[tuple]:
    rows = []
    for block in ("declared_contract", "draft", "rejected", "deferred"):
        for item in snapshot.get(block) or []:
            basis = item.get("basis") or {}
            rows.append((
                BLOCK_LABELS.get(block, block),
                item.get("child_table") or "",
                "、".join(item.get("child_columns") or []),
                item.get("parent_table") or "",
                "、".join(item.get("parent_columns") or []),
                item.get("conflict_type") or "",
                item.get("decision") or "",
                _side_text(basis.get("ddl")),
                _side_text(basis.get("sample")),
                item.get("decision_note") or "",
            ))
    return rows


def to_csv_text(headers, rows) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def publication_csv(snapshot: dict) -> bytes:
    return to_csv_text(SNAPSHOT_HEADERS, snapshot_rows(snapshot)).encode("utf-8-sig")
