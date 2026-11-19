// Sortable tables + video selection. No libraries, works without a build step.
(function () {
  function cellValue(row, idx, type) {
    const cell = row.children[idx];
    if (!cell) return null;
    const raw = cell.dataset.v !== undefined ? cell.dataset.v : cell.textContent.trim();
    if (type === "num") {
      if (raw === "" || raw === "None" || raw === "—") return null;
      const n = parseFloat(raw);
      return isNaN(n) ? null : n;
    }
    return raw === "None" ? "" : raw.toLowerCase();
  }
  document.querySelectorAll("table.sortable").forEach(function (table) {
    const headers = table.querySelectorAll("thead th");
    headers.forEach(function (th, idx) {
      const type = th.dataset.type;
      if (!type) return;
      th.classList.add("sort");
      th.tabIndex = 0;
      th.setAttribute("aria-sort", "none");
      function sort() {
        const dir = th.getAttribute("aria-sort") === "descending" ? "ascending" : "descending";
        headers.forEach(function (h) { if (h.dataset.type) h.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", dir);
        const body = table.tBodies[0];
        const rows = Array.from(body.rows);
        rows.sort(function (a, b) {
          const x = cellValue(a, idx, type), y = cellValue(b, idx, type);
          if (x === null && y === null) return 0;
          if (x === null) return 1;            // missing values always last
          if (y === null) return -1;
          const c = x < y ? -1 : x > y ? 1 : 0;
          return dir === "ascending" ? c : -c;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      }
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });

  const form = document.getElementById("video-form");
  if (form) {
    const boxes = function () { return form.querySelectorAll('input[name="ids"]'); };
    const update = function () {
      const n = form.querySelectorAll('input[name="ids"]:checked').length;
      const cmp = form.querySelector(".compare-btn");
      cmp.disabled = n < 2 || n > 6;
      cmp.textContent = n > 6 ? "Compare (max 6)" : "Compare selected" + (n ? " (" + n + ")" : "");
      form.querySelector(".bulk-btn").disabled = n < 1;
    };
    form.addEventListener("change", function (e) {
      if (e.target.classList.contains("select-all")) {
        boxes().forEach(function (b) { b.checked = e.target.checked; });
      }
      update();
    });
    update();
  }
})();
