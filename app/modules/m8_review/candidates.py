"""关系候选：把「DDL 声明」与「样本证实」两套依据并列，标出冲突，不互相覆盖。

两套依据来自不同权威源，各自独立采集：

- 声明依据：DDL 里的表级 FOREIGN KEY 或行内 REFERENCES。它是建表契约的一部分，
  样本缺值、父表没采到、采样噪声都不能让它从已发布规格里消失。
- 样本依据：对声明关系重算包含度（声明规则只看包含度），对未声明同名列按推断
  外键规则（包含度 ≥ 0.95 + 父列单列主键 + 基数比 ∈ [0.02, 0.95]）取证。

每条候选按「子表 + 子列 + 父表 + 父列」归一，两侧依据各自留快照，得到四种形态：

| 形态 | 含义 |
|---|---|
| `agreement` | 声明关系得到样本支撑（包含度 ≥ 0.95） |
| `declared_only` | 只有声明成立：样本没采到这一列、父表无样本或包含度不足 |
| `sample_only` | 样本证实但 DDL 未声明，可能是新关系，也可能是采样噪声 |
| `mismatch` | 同一子列在声明与样本里指向不同父键，必须人工裁定 |

`sample_only` 的候选会让同一子列出现「另一组完全包含的同名列」；
若同一子列既有声明又冒出这样的候选，整组标为 `mismatch`，两侧依据都保留。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ... import config, reasons
from ..m4_keys import keys

CONFLICT_ORDER = ("mismatch", "declared_only", "sample_only", "agreement")
DECISION_ORDER = ("pending", "accept", "reject", "defer")
SUPPORT_MIN = config.FK_INCLUSION_MIN


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def relation_id(child_table: str, child_columns: list, parent_table: str | None,
                parent_columns: list) -> str:
    return "|".join([
        "rel",
        (child_table or "").lower(),
        ",".join(str(name).lower() for name in child_columns or []),
        (parent_table or "").lower(),
        ",".join(str(name).lower() for name in parent_columns or []),
    ])


def _child_key(item: dict) -> tuple:
    return ((item.get("child_table") or "").lower(),
            tuple(str(name).lower() for name in item.get("child_columns") or []))


def _parent_key(item: dict) -> tuple:
    return ((item.get("parent_table") or "").lower(),
            tuple(str(name).lower() for name in item.get("parent_columns") or []))


def _parent_label(item: dict) -> str:
    parent = item.get("parent_table") or ""
    columns = ",".join(str(name) for name in item.get("parent_columns") or [])
    return f"{parent}.{columns}" if columns else parent


def _snapshot(finding: dict, *, source: str) -> dict:
    """一侧依据的完整快照：原始数值不做四舍五入，页面与导出共用。"""
    return {
        "source": source,
        "status": finding["status"],
        "inclusion": _text(finding.get("inclusion")),
        "cardinality_ratio": _text(finding.get("cardinality_ratio")),
        "reason_code": finding.get("reason_code", ""),
        "note": finding.get("note", ""),
        "evidence": dict(finding.get("evidence") or {}),
    }


def _column_values(coerced, table_name: str, column_name: str) -> tuple[list, bool]:
    table = coerced.table(table_name) if hasattr(coerced, "table") else None
    column = table.column(column_name) if table is not None else None
    if column is None or not column.present:
        return [], False
    return list(column.values), True


def sample_evidence(coerced, child_table: str, child_columns: list,
                    parent_table: str, parent_columns: list) -> dict:
    """对一条具体关系重算样本证据：子列/父列任一侧没采到就标 absent。"""
    if not child_columns or not parent_columns:
        return {"source": "sample", "method": "relation_inclusion",
                "status": "absent", "inclusion": None, "cardinality_ratio": None,
                "matched": 0, "child_distinct": 0, "parent_distinct": 0,
                "reason_code": "R_DECLARED_ONLY", "note": "关系缺少列名，无法取证"}
    child_values, child_present = _column_values(coerced, child_table,
                                                 child_columns[0])
    parent_values, parent_present = _column_values(coerced, parent_table,
                                                   parent_columns[0])
    if not child_present and not parent_present:
        return {"source": "sample", "method": "relation_inclusion",
                "status": "absent", "inclusion": None, "cardinality_ratio": None,
                "matched": 0, "child_distinct": 0, "parent_distinct": 0,
                "reason_code": "R_DECLARED_ONLY",
                "note": "样本表头里既没有子列也没有父列"}
    if not child_present:
        return {"source": "sample", "method": "relation_inclusion",
                "status": "absent", "inclusion": None, "cardinality_ratio": None,
                "matched": 0, "child_distinct": 0, "parent_distinct": 0,
                "reason_code": "R_DECLARED_ONLY",
                "note": "样本表头里没有子列，声明只能按 DDL 保留"}
    stats = keys.inclusion(child_values, parent_values)
    supported = (parent_present and stats["inclusion"] >= SUPPORT_MIN)
    return {
        "source": "sample", "method": "relation_inclusion",
        "status": "supported" if supported else "unsupported",
        "inclusion": _text(stats["inclusion"]),
        "cardinality_ratio": _text(stats["cardinality"]),
        "matched": stats["matched"],
        "child_distinct": stats["child_distinct"],
        "parent_distinct": stats["parent_distinct"],
        "reason_code": "" if supported else "R_DECLARED_ONLY",
        "note": (f"包含度 {_text(stats['inclusion'])}"
                 f"（{stats['matched']}/{stats['child_distinct']}），"
                 f"基数比 {_text(stats['cardinality'])}"),
    }


def _sample_relation(child, child_column, parent, parent_column,
                     pk_by_table: dict) -> dict | None:
    """样本侧独立采证：与 M4 推断外键同一套阈值，不参考声明。"""
    parent_pk = pk_by_table.get(parent.name.lower()) or {}
    names = [str(name).lower() for name in parent_pk.get("child_columns") or []]
    if len(names) != 1 or names[0] != parent_column.name.lower():
        return None
    if (child.name.lower() == parent.name.lower()
            and child_column.name.lower() == parent_column.name.lower()):
        return None
    stats = keys.inclusion(child_column.values, parent_column.values)
    status, code = keys._judge(stats, True, declared=False)
    if status != "inferred":
        return None
    return {
        "kind": "fk", "child_table": child.name,
        "child_columns": [child_column.name],
        "parent_table": parent.name, "parent_columns": [parent_column.name],
        "declared": False, "status": "inferred", "inclusion": stats["inclusion"],
        "cardinality_ratio": stats["cardinality"], "reason_code": code,
        "note": (f"包含度 {_text(stats['inclusion'])}"
                 f"（{stats['matched']}/{stats['child_distinct']}），"
                 f"基数比 {_text(stats['cardinality'])}"),
        "evidence": {"source": "sample",
                     **{key: _text(value) if isinstance(value, Decimal) else value
                        for key, value in stats.items()}},
    }


def sample_relations(coerced, pk_by_table: dict) -> list[dict]:
    """遍历所有同名列对，返回样本证实且 DDL 未声明的关系。"""
    out, seen = [], set()
    for child, child_column, parent, parent_column in keys.key_pairs(coerced.tables):
        finding = _sample_relation(child, child_column, parent, parent_column,
                                   pk_by_table)
        if finding is None:
            continue
        item_id = relation_id(child.name, [child_column.name], parent.name,
                              [parent_column.name])
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(finding)
    return out


def candidates(coerced, modeled) -> list[dict]:
    """合并两套依据，输出按固定顺序排列的候选清单（同输入逐字相同）。"""
    pk_by_table = {item["child_table"].lower(): item for item in modeled.keys
                   if item["kind"] == "pk"}
    declared = [item for item in modeled.keys
                if item["kind"] == "fk" and item["declared"]]
    samples = sample_relations(coerced, pk_by_table)
    evidence = {
        relation_id(item["child_table"], item["child_columns"],
                    item.get("parent_table"), item.get("parent_columns")):
        sample_evidence(coerced, item["child_table"], item["child_columns"],
                        item.get("parent_table") or "", item.get("parent_columns") or [])
        for item in declared
    }
    return merge(declared, samples, evidence)


def merge(declared: list[dict], samples: list[dict],
          evidence: dict | None = None) -> list[dict]:
    """两套依据的纯函数合并；测试直接喂合成结论验证四种冲突形态。

    `declared` 是 M4 的声明外键结论；`samples` 是样本证实但未声明的关系；
    `evidence` 是 {relation_id: 样本对声明关系的取证结果}。输出不含任何裁定。
    """
    evidence = evidence or {}
    groups: dict[tuple, dict[str, list]] = {}

    def bucket(item: dict) -> dict[str, list]:
        return groups.setdefault(_child_key(item),
                                 {"declared": [], "sample": []})

    for item in declared:
        bucket(item)["declared"].append(item)
    for item in samples:
        bucket(item)["sample"].append(item)

    rows: list[dict] = []
    for child_key in sorted(groups):
        group = groups[child_key]
        declared_group = sorted(group["declared"], key=_parent_key)
        sample_group = sorted(group["sample"], key=_parent_key)
        declared_ids = {relation_id(item["child_table"], item["child_columns"],
                                    item.get("parent_table"),
                                    item.get("parent_columns"))
                        for item in declared_group}
        sample_ids = {relation_id(item["child_table"], item["child_columns"],
                                  item.get("parent_table"),
                                  item.get("parent_columns"))
                      for item in sample_group}
        supported_declared = {
            item_id for item_id in declared_ids
            if (evidence.get(item_id) or {}).get("status") == "supported"
        }
        mismatch = bool(declared_ids and sample_ids
                        and declared_ids != sample_ids
                        and (declared_ids - supported_declared or
                             len(declared_ids | sample_ids) > 1))
        all_labels = sorted(_parent_label(item)
                            for item in declared_group + sample_group)

        def add(item: dict, *, ddl: dict | None, sample: dict | None,
                declared_sample: dict | None, conflict_type: str) -> None:
            item_id = relation_id(item["child_table"], item["child_columns"],
                                  item.get("parent_table"),
                                  item.get("parent_columns"))
            alternatives = sorted(label for label in all_labels
                                  if label != _parent_label(item))
            row = {
                "item_id": item_id,
                "kind": "relation",
                "child_table": item["child_table"],
                "child_columns": list(item["child_columns"]),
                "parent_table": item.get("parent_table") or "",
                "parent_columns": list(item.get("parent_columns") or []),
                "ddl": ddl,
                "sample": sample,
                "declared_sample": declared_sample,
                "conflict_type": conflict_type,
                "alternatives": alternatives,
                "allowed": list(config.REVIEW_DECISIONS),
                "reason_code": _reason_of(conflict_type),
            }
            row["conflict_detail"] = _detail(row)
            row["basis_note"] = _basis_note(row)
            rows.append(row)

        for item in declared_group:
            item_id = relation_id(item["child_table"], item["child_columns"],
                                  item.get("parent_table"),
                                  item.get("parent_columns"))
            sample = evidence.get(item_id)
            supported = (sample or {}).get("status") == "supported"
            if mismatch:
                conflict_type = "mismatch"
            elif supported:
                conflict_type = "agreement"
            else:
                conflict_type = "declared_only"
            presence = _presence(coerced=None, item=item, sample=sample)
            add(item, ddl=_snapshot(item, source="ddl"), sample=sample,
                declared_sample=presence, conflict_type=conflict_type)
        for item in sample_group:
            item_id = relation_id(item["child_table"], item["child_columns"],
                                  item.get("parent_table"),
                                  item.get("parent_columns"))
            if item_id in declared_ids:
                continue
            add(item, ddl=None, sample=_snapshot(item, source="sample"),
                declared_sample=None,
                conflict_type="mismatch" if mismatch else "sample_only")

    rows.sort(key=lambda row: (
        CONFLICT_ORDER.index(row["conflict_type"]),
        row["child_table"].lower(),
        ",".join(str(name).lower() for name in row["child_columns"]),
        row["parent_table"].lower(),
        ",".join(str(name).lower() for name in row["parent_columns"]),
    ))
    for ordinal, row in enumerate(rows, 1):
        row["ordinal"] = ordinal
        row["reason_code"] = _reason_of(row["conflict_type"])
    return rows


def _presence(*, coerced, item: dict, sample: dict | None) -> dict | None:
    sample = sample or {}
    if sample.get("status") == "absent":
        return {"present": False, "values": 0, "distinct": 0,
                "note": sample.get("note") or "样本里没有这一列"}
    if not sample:
        return None
    return {"present": True, "values": sample.get("child_distinct", 0),
            "distinct": sample.get("child_distinct", 0),
            "note": sample.get("note") or ""}


def _reason_of(conflict_type: str) -> str:
    return {
        "agreement": "R_AGREEMENT",
        "declared_only": "R_DECLARED_ONLY",
        "sample_only": "R_SAMPLE_ONLY",
        "mismatch": "R_MISMATCH",
    }[conflict_type]


def _detail(row: dict) -> str:
    ddl, sample = row.get("ddl") or {}, row.get("sample") or {}
    if row["conflict_type"] == "agreement":
        return f"DDL 声明得到样本支撑，包含度 {sample.get('inclusion') or '—'}"
    if row["conflict_type"] == "declared_only":
        if sample.get("status") == "absent":
            return (f"DDL 声明 → {row['parent_table']}，"
                    f"{sample.get('note') or '样本里没有取值'}")
        return (f"DDL 声明 → {row['parent_table']}，样本包含度 "
                f"{sample.get('inclusion') or '—'} 不足以支撑")
    if row["conflict_type"] == "sample_only":
        return (f"样本包含度 {sample.get('inclusion') or '—'} 指向 "
                f"{row['parent_table']}，DDL 未声明")
    others = "、".join(row.get("alternatives") or []) or "另一父键"
    return (f"同一子列两侧指向不同父键：声明 {ddl.get('inclusion') or '—'} / "
            f"样本 {sample.get('inclusion') or '—'}（另见 {others}）")


def _basis_note(row: dict) -> str:
    ddl, sample = row.get("ddl") or {}, row.get("sample") or {}
    parts = []
    if ddl:
        parts.append(f"声明 {ddl.get('status')}，包含度 {ddl.get('inclusion') or '—'}")
    if sample:
        parts.append(f"样本 {sample.get('status')}，包含度 "
                     f"{sample.get('inclusion') or '—'}")
    return "；".join(parts) or "—"


def conflict_counts(items: list[dict]) -> dict:
    counts = {name: 0 for name in CONFLICT_ORDER}
    for item in items:
        counts[item["conflict_type"]] = counts.get(item["conflict_type"], 0) + 1
    return counts


def decision_counts(items: list[dict]) -> dict:
    counts = {name: 0 for name in DECISION_ORDER}
    for item in items:
        decision = item.get("decision") or "pending"
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def item_signature(item: dict) -> str:
    """单条依据签名：两侧原始依据 + 冲突形态。裁定是否可沿用只看它。"""
    payload = {
        "item_id": item["item_id"],
        "child_table": item["child_table"],
        "child_columns": list(item["child_columns"]),
        "parent_table": item["parent_table"],
        "parent_columns": list(item["parent_columns"]),
        "conflict_type": item["conflict_type"],
        "ddl": item.get("ddl"),
        "sample": item.get("sample"),
        "declared_sample": item.get("declared_sample"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def signature(items: list[dict]) -> list[dict]:
    """依据签名：只含两侧原始依据与形态，不含裁定，供依据摘要复算。"""
    return [{
        "item_id": item["item_id"],
        "kind": item["kind"],
        "child_table": item["child_table"],
        "child_columns": list(item["child_columns"]),
        "parent_table": item["parent_table"],
        "parent_columns": list(item["parent_columns"]),
        "conflict_type": item["conflict_type"],
        "ddl": item.get("ddl"),
        "sample": item.get("sample"),
        "declared_sample": item.get("declared_sample"),
    } for item in items]


def label_of(item: dict) -> str:
    return reasons.label(item.get("reason_code") or _reason_of(item["conflict_type"]))
