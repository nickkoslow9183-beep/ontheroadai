// Video Studio: chunked upload with progress, live job status, clip editor helpers. No libraries.
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const LABELS = { queued: "Queued", processing: "Processing", needs_review: "Needs review", failed: "Failed",
                   completed: "Completed", cancelled: "Cancelled" };

  // ---- upload: 8 MB pieces, so large files never have to fit in memory ----
  const form = $("#upload-form");
  if (form) {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const file = form.file.files[0];
      if (!file) return;
      const box = $(".upload-progress", form), bar = $("progress", box), text = $(".upload-text", box);
      const btn = $("button[type=submit]", form);
      box.hidden = false; btn.disabled = true;
      const fail = (msg) => { text.textContent = "Upload failed: " + msg; text.classList.add("err"); btn.disabled = false; };
      const meta = Object.fromEntries(new FormData(form).entries());
      delete meta.file;
      try {
        let r = await fetch(form.dataset.init || "/studio/upload/init", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...meta, filename: file.name, size: file.size }) });
        let j = await r.json();
        if (!r.ok) return fail(j.error || r.statusText);
        const token = j.token, chunk = j.chunk_size;
        let offset = 0;
        const t0 = Date.now();
        while (offset < file.size) {
          const piece = file.slice(offset, offset + chunk);
          let tries = 0, ok = false;
          while (!ok && tries < 3) {
            tries++;
            try {
              r = await fetch(`/studio/upload/${token}?offset=${offset}`, { method: "PUT", body: piece });
              j = await r.json();
              ok = r.ok;
              if (!ok && !/offset/i.test(j.error || "")) return fail(j.error || r.statusText);
              if (!ok) { offset = parseInt((j.error.match(/resume from (\d+)/) || [])[1] || offset, 10); ok = true; continue; }
              offset = j.received;
            } catch (e) { if (tries >= 3) return fail("network error, try again"); }
          }
          const pct = Math.round(offset / file.size * 100);
          const mbps = offset / 1e6 / Math.max(1, (Date.now() - t0) / 1000);
          bar.value = pct;
          text.textContent = `Uploading ${pct}% (${(offset / 1e6).toFixed(0)} of ${(file.size / 1e6).toFixed(0)} MB, ${mbps.toFixed(1)} MB/s)`;
        }
        text.textContent = "Checking the video with FFmpeg…";
        r = await fetch(`/studio/upload/${token}/complete`, { method: "POST" });
        j = await r.json();
        if (!r.ok) return fail(j.error || r.statusText);
        text.textContent = "Uploaded. Opening the recording…";
        window.location = j.url;
      } catch (e) { fail(e.message || String(e)); }
    });
  }

  // ---- live job status ----
  $$("table.jobs[data-poll]").forEach((table) => {
    const active = () => $$(".job-status", table).some((b) => /Queued|Processing/.test(b.textContent));
    if (!active()) return;
    const reload = table.dataset.reloadWhenDone;
    const timer = setInterval(async () => {
      try {
        const r = await fetch(table.dataset.poll);
        const jobs = await r.json();
        let finished = false;
        jobs.forEach((j) => {
          const row = $(`tr[data-job="${j.id}"]`, table);
          if (!row) return;
          const badge = $(".job-status", row);
          const was = badge.textContent;
          badge.textContent = LABELS[j.status] || j.status;
          badge.className = `badge s-${j.status} job-status`;
          $(".job-progress", row).value = j.progress;
          $(".job-message", row).firstChild.textContent = j.message;
          if (/Queued|Processing/.test(was) && !/queued|processing/.test(j.status)) finished = true;
        });
        if (finished && (reload || !active())) { clearInterval(timer); window.location.reload(); }
      } catch (e) { /* keep polling */ }
    }, 2000);
  });

  // ---- transcript / clip editor: jump to a time ----
  const vid = $("#cvideo") || $("#tvideo") || $(".source-video");
  $$(".jump").forEach((b) => b.addEventListener("click", () => { if (vid) { vid.currentTime = parseFloat(b.dataset.t); vid.play(); } }));

  // ---- clip editor ----
  const cf = $("#clip-form");
  if (cf && vid) {
    const start = $("#start"), end = $("#end"), dur = $("#dur");
    const upd = () => { dur.textContent = (parseFloat(end.value) - parseFloat(start.value)).toFixed(1) + "s"; };
    start.addEventListener("input", upd); end.addEventListener("input", upd);
    $("#set-start").addEventListener("click", () => { start.value = vid.currentTime.toFixed(2); upd(); });
    $("#set-end").addEventListener("click", () => { end.value = vid.currentTime.toFixed(2); upd(); });
    $$(".use-start").forEach((b) => b.addEventListener("click", () => { start.value = b.dataset.t; upd(); }));
    $$(".use-end").forEach((b) => b.addEventListener("click", () => { end.value = b.dataset.t; upd(); }));
    let stopAt = null;
    $("#play-clip").addEventListener("click", () => { vid.currentTime = parseFloat(start.value); stopAt = parseFloat(end.value); vid.play(); });
    vid.addEventListener("timeupdate", () => { if (stopAt !== null && vid.currentTime >= stopAt) { vid.pause(); stopAt = null; } drawCrop(); });
    $("#add-kf").addEventListener("click", () => {
      const rows = $$(".kf", cf), last = rows[rows.length - 1], copy = last.cloneNode(true);
      $("input[name=kf_t]", copy).value = "";
      last.after(copy); wireKf(copy);
    });
    const wireKf = (row) => $(".kf-now", row).addEventListener("click", () => {
      $("input[name=kf_t]", row).value = Math.max(0, vid.currentTime - parseFloat(start.value)).toFixed(2); drawCrop();
    });
    $$(".kf", cf).forEach(wireKf);

    // crop preview: shows roughly what the 9:16 frame keeps at the current playback time
    const sw = parseFloat(cf.dataset.srcW), sh = parseFloat(cf.dataset.srcH);
    const boxes = $$(".cropbox");
    const activeMode = () => {
      let mode = $("#mode").value, x = parseFloat($("#x").value);
      const t = vid.currentTime - parseFloat(start.value);
      $$(".kf", cf).forEach((row) => {
        const kt = parseFloat($("input[name=kf_t]", row).value);
        if (!isNaN(kt) && kt <= t) { mode = $("select[name=kf_mode]", row).value; x = parseFloat($("input[name=kf_x]", row).value); }
      });
      return { mode, x };
    };
    function drawCrop() {
      if (!vid.videoWidth) return;
      const r = vid.getBoundingClientRect();
      const scale = Math.min(r.width / sw, r.height / sh);
      const ow = sw * scale, oh = sh * scale, ox = (r.width - ow) / 2, oy = (r.height - oh) / 2;
      const { mode, x } = activeMode();
      const lx = parseFloat($("#left_x").value), rx = parseFloat($("#right_x").value);
      const place = (b, cx, wFrac, hFrac, cy) => {
        const w = ow * wFrac, h = oh * hFrac;
        const left = Math.max(0, Math.min(ow - w, cx * ow - w / 2));
        const top = Math.max(0, Math.min(oh - h, cy * oh - h / 2));
        Object.assign(b.style, { display: "block", left: ox + left + "px", top: oy + top + "px", width: w + "px", height: h + "px" });
      };
      boxes.forEach((b) => (b.style.display = "none"));
      if (mode === "both") { place(boxes[0], 0.5, 1, 1, 0.5); return; }
      if (mode === "split") {
        const pw = Math.min(0.5, (sh * 1080 / 960) / sw), ph = (pw * sw) / (1080 / 960) / sh;
        place(boxes[0], lx, pw, ph, 0.45); place(boxes[1], rx, pw, ph, 0.45); return;
      }
      const cx = mode === "left" ? lx : mode === "right" ? rx : mode === "center" ? 0.5 : x;
      const wf = Math.min(1, (sh * 9 / 16) / sw);
      place(boxes[0], cx, wf, wf < 1 ? 1 : (sw * 16 / 9) / sh, 0.5);
    }
    ["#mode", "#x", "#left_x", "#right_x"].forEach((s) => $(s).addEventListener("input", drawCrop));
    cf.addEventListener("input", drawCrop);
    vid.addEventListener("loadedmetadata", drawCrop);
    window.addEventListener("resize", drawCrop);
  }
})();
