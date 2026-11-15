"""M8 入口：裁定包查询、逐条裁定、整版发布、发布版本、中断续跑与页面。

所有判定都在 service.py；本文件只做参数校验、错误码与渲染。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response

from ... import config, errors, reasons
from ...web import Ctx, get_ctx, render
from . import export, service

router = APIRouter(tags=["M8 契约裁定与发布"])


def _batch_id(ctx: Ctx, batch_id: str | None) -> str:
    if batch_id:
        batch = ctx.store.get_batch(batch_id)
        if batch is None:
            raise errors.NotFound(f"批次不存在：{batch_id}", code="B_EMPTY")
        return batch["batch_id"]
    batch = ctx.current_batch()
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先跑一次接入", code="B_EMPTY")
    return batch["batch_id"]


def _review_or_404(ctx: Ctx, review_id: str) -> dict:
    review = ctx.store.get_review(review_id)
    if review is None:
        raise errors.NotFound(f"裁定包不存在：{review_id}", code="R_UNKNOWN_ITEM")
    return review


@router.get("/api/review")
def api_review(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    """裁定包视图：两套依据、冲突标记、逐条裁定状态与当前发布版本。"""
    resolved = _batch_id(ctx, batch_id)
    payload = service.current(ctx.store, ctx.settings, resolved)
    payload["reason_labels"] = {code: reasons.label(code)
                               for code in ("R_AGREEMENT", "R_DECLARED_ONLY",
                                            "R_SAMPLE_ONLY", "R_MISMATCH")}
    return payload


@router.get("/api/review/{review_id}")
def api_review_one(review_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    review = _review_or_404(ctx, review_id)
    payload = service.view(ctx.store, review)
    payload["review"] = review
    return payload


@router.post("/api/review/{review_id}/decide")
def api_decide(review_id: str, payload: dict = Body(default_factory=dict),
               ctx: Ctx = Depends(get_ctx)) -> dict:
    """逐条裁定：可一次提交多条；已裁项可改写，裁定与注记立即落库。"""
    _review_or_404(ctx, review_id)
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    return service.decide(ctx.store, review_id, decisions)


@router.post("/api/review/{review_id}/publish")
def api_publish(review_id: str, payload: dict = Body(default_factory=dict),
                ctx: Ctx = Depends(get_ctx)) -> dict:
    """整版发布：可内联裁定包（basis_digest + decisions），幂等重放不新增版本。"""
    review = _review_or_404(ctx, review_id)
    return service.publish(ctx.store, ctx.settings, review["batch_id"],
                           package=payload if isinstance(payload, dict) else {})


@router.get("/api/publications")
def api_publications(review_id: str | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.publications(ctx.store, review_id)


@router.get("/api/publications/{publication_id}")
def api_publication(publication_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.publication_detail(ctx.store, publication_id)


@router.get("/api/publications/{publication_id}/export.csv")
def api_publication_csv(publication_id: str, ctx: Ctx = Depends(get_ctx)) -> Response:
    detail = service.publication_detail(ctx.store, publication_id)
    return Response(content=export.publication_csv(detail["snapshot"]),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="starmodel-contract-{publication_id}.csv"'})


@router.get("/api/review/{review_id}/package.json")
def api_review_package(review_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """裁定包：basis_digest + 逐条裁定，重新提交即得到同一发布版本。"""
    review = _review_or_404(ctx, review_id)
    payload = service.view(ctx.store, review)
    return {
        "review_id": review_id,
        "batch_id": review["batch_id"],
        "basis_digest": review["basis_digest"],
        "decisions": [{"item_id": item["item_id"], "decision": item["decision"],
                       "note": item["decision_note"]}
                      for item in payload["items"]],
    }


@router.post("/api/review/{review_id}/resume")
def api_resume(review_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """中断后续跑：已裁定项原样沿用，只列出未裁项。"""
    review = _review_or_404(ctx, review_id)
    return service.resume(ctx.store, ctx.settings, review["batch_id"])


@router.get("/review")
def page_review(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    payload = None
    review = None
    if batch is not None:
        payload = service.current(ctx.store, ctx.settings, batch["batch_id"])
        review = payload["review"]
        for item in payload["items"]:
            if item.get("ddl"):
                item["ddl"]["status_label"] = _side_label(item["ddl"].get("status"))
            if item.get("sample"):
                item["sample"]["status_label"] = _side_label(
                    item["sample"].get("status"))
    return render(request, "review.html", page="review", page_title="契约裁定与发布",
                  payload=payload, review=review, batch=batch,
                  decision_labels=config.REVIEW_DECISION_LABELS,
                  conflict_labels=config.REVIEW_CONFLICT_LABELS)


def _side_label(status) -> str:
    return {
        "confirmed": "成立", "inferred": "推断成立", "unidirectional": "单向",
        "declared_only": "仅声明", "sample_only": "仅样本", "rejected": "不成立",
        "unresolved": "未解析",
    }.get(status or "", status or "—")
