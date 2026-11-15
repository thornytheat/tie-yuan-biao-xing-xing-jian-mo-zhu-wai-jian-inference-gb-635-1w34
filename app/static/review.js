/* review.js — 契约裁定页：逐条裁定、整版发布、续跑。判定全在后端。 */
(function () {
  const state = { running: false, reviewId: "", basisDigest: "" };

  function noticeNode() {
    return document.querySelector("[data-notice]");
  }

  function resultNode() {
    return document.querySelector("[data-result]");
  }

  function showNotice(text, tone) {
    const node = noticeNode();
    if (!node) {
      return;
    }
    node.textContent = text;
    node.dataset.tone = tone || "";
    node.hidden = false;
  }

  function showResult(text, tone) {
    const node = resultNode();
    if (!node) {
      return;
    }
    node.textContent = text;
    node.dataset.tone = tone || "";
    node.hidden = false;
  }

  async function request(url, method, payload) {
    const response = await fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const code = body.error ? `${body.error}：` : "";
      throw new Error(`${code}${body.message || `HTTP ${response.status}`}`);
    }
    return body;
  }

  async function bootstrap() {
    if (!state.reviewId) {
      return;
    }
    const payload = await request(`/api/review/${state.reviewId}`, "GET");
    state.basisDigest = payload.review ? payload.review.basis_digest : "";
  }

  async function decideOne(button) {
    if (state.running) {
      return;
    }
    const itemId = button.dataset.itemId;
    const decision = button.dataset.decide;
    state.running = true;
    button.disabled = true;
    try {
      const body = await request(`/api/review/${state.reviewId}/decide`, "POST", {
        decisions: [{ item_id: itemId, decision: decision }],
      });
      const label = body.labels.decision[decision];
      showResult(`已${label} 1 条，未裁定 ${body.decisions.pending} 条`, "ok");
      window.setTimeout(() => window.location.reload(), 240);
    } catch (error) {
      showNotice(String(error.message), "bad");
    } finally {
      state.running = false;
      button.disabled = false;
    }
  }

  async function acceptAgreements(button) {
    if (state.running) {
      return;
    }
    const items = Array.from(document.querySelectorAll("tr[data-item]"))
      .filter((row) => row.querySelector("[data-decide]"))
      .filter((row) => row.textContent.includes("两侧一致"))
      .map((row) => row.dataset.item);
    if (items.length === 0) {
      showNotice("没有两侧一致的候选。", "warn");
      return;
    }
    state.running = true;
    button.disabled = true;
    try {
      const body = await request(`/api/review/${state.reviewId}/decide`, "POST", {
        decisions: items.map((itemId) => ({
          item_id: itemId,
          decision: "accept",
          note: "两侧一致，采纳",
        })),
      });
      showResult(`采纳 ${items.length} 条，未裁定 ${body.decisions.pending} 条`, "ok");
      window.setTimeout(() => window.location.reload(), 240);
    } catch (error) {
      showNotice(String(error.message), "bad");
    } finally {
      state.running = false;
      button.disabled = false;
    }
  }

  async function publish(button) {
    if (state.running) {
      return;
    }
    state.running = true;
    button.disabled = true;
    button.textContent = "发布中";
    try {
      const body = await request(`/api/review/${state.reviewId}/publish`, "POST", {});
      const tone = body.replayed ? "info" : "ok";
      showResult(
        `${body.replayed ? "重放" : "发布"} v${body.version_no}`
          + ` · 声明契约 ${body.snapshot.counts.declared_contract}`
          + ` · 建模草稿 ${body.snapshot.counts.draft}`,
        tone,
      );
      window.setTimeout(() => window.location.reload(), 420);
    } catch (error) {
      showNotice(String(error.message), "bad");
    } finally {
      state.running = false;
      button.disabled = false;
      button.textContent = "整版发布";
    }
  }

  async function resume(button) {
    if (state.running) {
      return;
    }
    state.running = true;
    button.disabled = true;
    try {
      const body = await request(`/api/review/${state.reviewId}/resume`, "POST");
      showResult(body.note, body.pending_count ? "warn" : "ok");
    } catch (error) {
      showNotice(String(error.message), "bad");
    } finally {
      state.running = false;
      button.disabled = false;
    }
  }

  function bind() {
    const page = document.querySelector("[data-review-id]");
    if (page) {
      state.reviewId = page.dataset.reviewId;
      state.basisDigest = page.dataset.basisDigest || "";
    } else {
      const body = document.body;
      const match = window.location.search.match(/[?&]review_id=([^&]+)/);
      if (match) {
        state.reviewId = decodeURIComponent(match[1]);
      }
    }
    document.querySelectorAll("[data-decide]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        decideOne(button);
      });
    });
    const acceptAll = document.querySelector("[data-action=accept-all]");
    if (acceptAll) {
      acceptAll.addEventListener("click", (event) => {
        event.preventDefault();
        acceptAgreements(acceptAll);
      });
    }
    const publishButton = document.querySelector("[data-action=publish]");
    if (publishButton) {
      publishButton.addEventListener("click", (event) => {
        event.preventDefault();
        publish(publishButton);
      });
    }
    const resumeButton = document.querySelector("[data-action=resume]");
    if (resumeButton) {
      resumeButton.addEventListener("click", (event) => {
        event.preventDefault();
        resume(resumeButton);
      });
    }
  }

  document.addEventListener("DOMContentLoaded", bind);
})();
