/**
 * article.js — Wikipedia article resolution and domain map polling.
 */

const Article = (() => {
  let _pollInterval = null;

  async function resolve(urlOrTitle) {
    const data = await apiFetch("/api/articles/resolve", {
      method: "POST",
      body: JSON.stringify({ url: urlOrTitle }),
    });
    return data; // { article_id, title, domain_map_status, ... }
  }

  async function get(articleId) {
    return apiFetch(`/api/articles/${articleId}`);
  }

  /**
   * Poll until domain_map_status == "ready" or "failed", or timeout.
   * Returns the article data when ready.
   */
  function poll(articleId, { intervalMs = 3000, timeoutMs = 120000 } = {}) {
    return new Promise((resolve, reject) => {
      const start = Date.now();

      _pollInterval = setInterval(async () => {
        if (Date.now() - start > timeoutMs) {
          clearInterval(_pollInterval);
          return reject(new Error("Timed out waiting for article analysis"));
        }
        try {
          const data = await get(articleId);
          if (data.domain_map_status === "ready") {
            clearInterval(_pollInterval);
            return resolve(data);
          }
          if (data.domain_map_status === "failed") {
            clearInterval(_pollInterval);
            return reject(new Error("Article analysis failed"));
          }
        } catch (e) {
          clearInterval(_pollInterval);
          return reject(e);
        }
      }, intervalMs);
    });
  }

  function cancelPoll() {
    if (_pollInterval) {
      clearInterval(_pollInterval);
      _pollInterval = null;
    }
  }

  function renderCard(data) {
    document.getElementById("article-title").textContent   = data.title || data.canonical_title || "";
    const summary = data.summary || "";
    document.getElementById("article-summary").textContent =
      summary.length > 300 ? summary.slice(0, 300) + "…" : summary;
    document.getElementById("article-kc-count").textContent =
      data.kc_count ? `${data.kc_count} knowledge concepts identified` : "";
    document.getElementById("btn-start-session").disabled = !data.kc_count;
    document.getElementById("article-card").classList.remove("hidden");
  }

  return { resolve, get, poll, cancelPoll, renderCard };
})();
