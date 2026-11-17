/* Interval Ledger - UI behaviour.  No framework, no build step. */

const state = {
  epoch: "2026-11-01T00:00:00Z",
  tickSeconds: 60,
  sources: [],
  sourceId: null,
  head: 0,
  lo: 0,
  hi: 480,
  granularity: 5,
  includeRevoked: false,
  data: null,
  impact: null,
  verify: null,
  auditSeq: null,
  audit: null,
  draftSeq: null,
  draftDirty: false,
  localDraft: null,
  toastTimer: null,
};

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US");

/* ---------- time helpers ---------- */

function isoToTick(iso) {
  const [h, m] = iso.split(":").map(Number);
  return h * 60 + m;
}

function tickToLabel(tick) {
  const d = new Date(Date.parse(state.epoch) + tick * state.tickSeconds * 1000);
  const iso = d.toISOString();
  return iso.slice(11, 16);
}

function tickToDateTime(tick) {
  const d = new Date(Date.parse(state.epoch) + tick * state.tickSeconds * 1000);
  return d.toISOString().slice(0, 16).replace("T", " ") + "Z";
}

function spanLabel(lo, hi) {
  return `[${lo}, ${hi}) · ${tickToLabel(lo)}–${tickToLabel(hi)}`;
}

function ticks(n) {
  return `${fmt.format(n)} tick${n === 1 ? "" : "s"}`;
}

function duration(n) {
  if (n < 60) return `${n} min`;
  const h = Math.floor(n / 60);
  const m = n % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

/* ---------- transport ---------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    ...options,
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const err = new Error(body?.error?.message || `Request failed (${response.status})`);
    err.status = response.status;
    err.code = body?.error?.code;
    err.details = body?.error?.details;
    throw err;
  }
  return body;
}

function toast(message, kind = "info") {
  const el = $("toast");
  el.textContent = message;
  el.dataset.kind = kind;
  el.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    el.hidden = true;
  }, kind === "error" ? 6500 : 3200);
}

/* ---------- data load ---------- */

async function loadSources(selectFirst = true) {
  const body = await api("/api/query/sources");
  state.sources = body.sources.filter((s) => s.head_seq > 0);
  if (!state.sources.length) {
    $("app").classList.add("is-empty");
    $("source-select").innerHTML = `<option value="">No sources with data</option>`;
    return;
  }
  $("app").classList.remove("is-empty");
  const keep = state.sourceId;
  $("source-select").innerHTML = state.sources
    .map(
      (s) =>
        `<option value="${s.id}">${escapeHtml(s.name)} · ${fmt.format(s.head_seq)} entries</option>`
    )
    .join("");
  if (keep && state.sources.some((s) => s.id === keep)) {
    $("source-select").value = String(keep);
  } else if (selectFirst) {
    state.sourceId = state.sources[0].id;
    $("source-select").value = String(state.sourceId);
  }
  const source = state.sources.find((s) => s.id === state.sourceId);
  if (source) {
    $("source-meta").textContent = `${source.description || "no description"} · ${source.observation_count} observations · ${source.revision_count} revisions`;
    state.epoch = body.epoch || state.epoch;
  }
}

async function loadState() {
  if (state.sourceId == null) return;
  const params = new URLSearchParams({
    lo: state.lo,
    hi: state.hi,
    granularity: state.granularity,
    expand: "1",
  });
  if (state.includeRevoked) params.set("include_revoked", "1");
  const view = await api(`/api/merger/sources/${state.sourceId}/state?${params}`);
  state.data = view;
  state.impact = view;
  state.head = view.head_seq;
  render();
}

/* ---------- rendering ---------- */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render() {
  const data = state.data;
  if (!data) return;
  $("head-badge").textContent = `head ${data.head_seq}`;
  renderKpis(data);
  renderChart(data);
  renderCoverage(data);
  renderSegments(data);
  renderLedger(data);
  renderDraftState();
  renderCost(data);
}

function renderKpis(data) {
  const compressed = state.impact;
  if (!compressed) return;
  const total = compressed.totals.reduce((a, b) => a + b, 0);
  const covered = compressed.buckets.reduce((a, b) => a + b.covered, 0);
  const span = state.hi - state.lo;
  $("kpi-total").textContent = fmt.format(total);
  $("kpi-total-note").textContent = `Σ value × ticks over ${duration(span)}`;
  $("kpi-covered").textContent = `${Math.round((covered / span) * 100)}%`;
  $("kpi-covered-note").textContent = `${fmt.format(covered)} of ${fmt.format(span)} ticks observed`;
  $("kpi-segments").textContent = fmt.format(compressed.cost.segments_scanned);
  $("kpi-segments-note").textContent = `runs scanned for ${fmt.format(compressed.buckets.length)} buckets`;
  const revokedNote = data.revoked_revisions.length
    ? `${data.revoked_revisions.length} revoked · ${state.includeRevoked ? "shown" : "excluded"}`
    : "none revoked";
  $("kpi-revoked").textContent = data.revoked_revisions.length || "0";
  $("kpi-revoked-note").textContent = revokedNote;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function renderChart(data) {
  const host = $("chart");
  host.innerHTML = "";
  const buckets = state.impact?.buckets || [];
  if (!buckets.length) {
    host.innerHTML = `<p class="muted">No buckets in this window.</p>`;
    return;
  }
  const width = Math.max(640, host.clientWidth || 900);
  const height = 240;
  const pad = { top: 18, right: 16, bottom: 30, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(...buckets.map((b) => b.total), 1);
  const scaleY = (v) => pad.top + plotH - (v / max) * plotH;
  const barW = plotW / buckets.length;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Integral per ${state.granularity}-tick bucket across the query window`);

  const ticksY = niceTicks(max);
  for (const value of ticksY) {
    const y = scaleY(value);
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", pad.left);
    line.setAttribute("x2", width - pad.right);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "grid");
    svg.appendChild(line);
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", pad.left - 8);
    label.setAttribute("y", y + 4);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", "axis");
    label.textContent = compact(value);
    svg.appendChild(label);
  }

  const tooltip = $("chart-tip");
  buckets.forEach((bucket, i) => {
    const x = pad.left + i * barW;
    const y = scaleY(bucket.total);
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", x + Math.min(1, barW * 0.12));
    rect.setAttribute("y", y);
    rect.setAttribute("width", Math.max(1, barW - Math.min(2, barW * 0.24)));
    rect.setAttribute("height", Math.max(0, pad.top + plotH - y));
    rect.setAttribute("rx", Math.min(3, barW * 0.2));
    rect.setAttribute("class", "bar");
    rect.dataset.index = i;
    if (bucket.covered < bucket.ticks) rect.classList.add("partial");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${spanLabel(bucket.start, bucket.end)}\n∫ ${fmt.format(bucket.total)} · ${bucket.covered}/${bucket.ticks} ticks covered`;
    rect.appendChild(title);
    svg.appendChild(rect);
  });

  // x labels: first, middle, last bucket start
  const labelIndexes = [0, Math.floor(buckets.length / 2), buckets.length - 1];
  for (const index of labelIndexes) {
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", pad.left + index * barW + barW / 2);
    label.setAttribute("y", height - 10);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "axis");
    label.textContent = tickToLabel(buckets[index].start);
    svg.appendChild(label);
  }

  host.appendChild(svg);
  host.onmousemove = (event) => {
    const target = event.target.closest?.(".bar");
    if (!target) {
      tooltip.hidden = true;
      return;
    }
    const bucket = buckets[Number(target.dataset.index)];
    tooltip.innerHTML = `<strong>${spanLabel(bucket.start, bucket.end)}</strong><span>∫ ${fmt.format(bucket.total)}</span><span>${bucket.covered}/${bucket.ticks} ticks covered</span>`;
    tooltip.hidden = false;
    const hostRect = host.getBoundingClientRect();
    tooltip.style.left = `${Math.min(hostRect.width - 200, event.clientX - hostRect.left + 12)}px`;
    tooltip.style.top = `${Math.max(0, event.clientY - hostRect.top - 60)}px`;
  };
  host.onmouseleave = () => {
    tooltip.hidden = true;
  };
}

function niceTicks(max) {
  const raw = max / 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = 0; v <= max + step; v += step) out.push(v);
  return out;
}

function compact(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}K`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return String(Math.round(value));
}

function renderCoverage(data) {
  const host = $("coverage");
  host.innerHTML = "";
  const segments = (state.impact?.segments || []).filter((s) => s.end > state.lo && s.start < state.hi);
  if (!segments.length) {
    host.innerHTML = `<p class="muted">No observations in this window.</p>`;
    return;
  }
  const lo = state.lo;
  const span = state.hi - state.lo;
  let cursor = lo;
  const rows = [];
  for (const seg of segments) {
    if (seg.start > cursor) {
      rows.push({ kind: "gap", start: cursor, end: seg.start });
    }
    rows.push({ kind: "run", ...seg });
    cursor = Math.max(cursor, seg.end);
  }
  if (cursor < state.hi) rows.push({ kind: "gap", start: cursor, end: state.hi });
  const maxValue = Math.max(...segments.map((s) => s.value), 1);
  const html = rows
    .map((row) => {
      const left = ((row.start - lo) / span) * 100;
      const width = ((row.end - row.start) / span) * 100;
      if (row.kind === "gap") {
        return `<div class="cov-gap" style="left:${left}%;width:${width}%" title="source gap ${spanLabel(row.start, row.end)}"></div>`;
      }
      const alpha = 0.25 + (row.value / maxValue) * 0.7;
      return `<div class="cov-run" style="left:${left}%;width:${width}%;--v:${alpha.toFixed(3)}" title="value ${row.value} ${spanLabel(row.start, row.end)} · from entry ${row.source_entry}"></div>`;
    })
    .join("");
  host.innerHTML = `
    <div class="cov-strip" role="img" aria-label="Value and coverage strip for the window">${html}</div>
    <div class="cov-axis"><span>${tickToLabel(lo)}</span><span>${tickToLabel((lo + state.hi) / 2)}</span><span>${tickToLabel(state.hi)}</span></div>
    <div class="cov-legend"><span class="legend-dot"></span> run (darker = higher value) <span class="legend-gap"></span> gap: no observation, not zero</div>`;
}

function renderSegments(data) {
  const rows = state.impact?.segments || [];
  const body = $("segment-rows");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No runs in this window.</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((seg) => {
      const selected = state.auditSeq === seg.source_entry;
      return `<tr class="${selected ? "is-selected" : ""}">
        <td class="num">${fmt.format(seg.start)}</td>
        <td class="num">${fmt.format(seg.end)}</td>
        <td class="num">${seg.ticks}</td>
        <td class="num">${fmt.format(seg.value)}</td>
        <td class="num">${fmt.format(seg.value * seg.ticks)}</td>
        <td><button class="link" data-audit="${seg.source_entry}">#${seg.source_entry}</button>
          <span class="kind-tag ${seg.source_kind === "revision" ? "rev" : ""}">${seg.source_kind}</span></td>
      </tr>`;
    })
    .join("");
  for (const button of body.querySelectorAll("[data-audit]")) {
    button.addEventListener("click", () => audit(Number(button.dataset.audit)));
  }
}

function renderLedger(data) {
  const host = $("ledger");
  const entries = [...(data.entries || [])].reverse();
  if (!entries.length) {
    host.innerHTML = `<p class="muted">The ledger is empty.</p>`;
    return;
  }
  host.innerHTML = entries
    .map((entry) => {
      const span = entry.kind === "observation"
        ? (entry.runs || []).map((r) => `[${r.start}, ${r.end}) = ${r.value}`).join(" ")
        : `[${entry.start}, ${entry.end}) = ${entry.value}`;
      const revoked = entry.kind === "revision" && entry.revoked_by;
      const classes = ["entry", entry.kind];
      if (revoked) classes.push("revoked");
      if (entry.kind === "revision" && !revoked) classes.push("active");
      if (state.auditSeq === entry.seq) classes.push("is-selected");
      return `<li class="${classes.join(" ")}">
        <div class="entry-head">
          <span class="seq">#${entry.seq}</span>
          <span class="kind">${entry.kind}</span>
          ${revoked ? `<span class="revoked-tag">revoked by #${entry.revoked_by}</span>` : ""}
          ${entry.kind === "revision" && !revoked ? `<span class="live-tag">active</span>` : ""}
        </div>
        <div class="entry-span mono">${escapeHtml(span)}</div>
        ${entry.note ? `<div class="entry-note">${escapeHtml(entry.note)}</div>` : ""}
        <div class="entry-actions">
          ${entry.kind === "revision" ? `<button class="link" data-audit="${entry.seq}">split basis</button>` : ""}
          ${entry.kind === "revision" && !revoked ? `<button class="link" data-revoke="${entry.seq}">revoke</button>` : ""}
          ${entry.kind === "observation" || entry.kind === "revision" ? `<button class="link" data-load="${entry.seq}">load into editor</button>` : ""}
        </div>
      </li>`;
    })
    .join("");
  for (const button of host.querySelectorAll("[data-audit]")) {
    button.addEventListener("click", () => audit(Number(button.dataset.audit)));
  }
  for (const button of host.querySelectorAll("[data-revoke]")) {
    button.addEventListener("click", () => revoke(Number(button.dataset.revoke)));
  }
  for (const button of host.querySelectorAll("[data-load]")) {
    button.addEventListener("click", () => loadIntoEditor(Number(button.dataset.load)));
  }
}

function renderCost(data) {
  if (!data) return;
  const compressed = data.cost;
  const expandedTicks = compressed.ticks_expanded || 0;
  const span = state.hi - state.lo;
  const rows = [];
  rows.push({
    label: "compressed",
    segments: compressed.segments_scanned,
    ticks: `${fmt.format(compressed.segments_scanned)} runs`,
    bytes: `${fmt.format(compressed.segments_scanned * 40)} B est.`,
    ms: compressed.compressed_ms,
  });
  if (expandedTicks) {
    rows.push({
      label: "expanded",
      segments: span,
      ticks: `${fmt.format(expandedTicks)} rows`,
      bytes: `${fmt.format(expandedTicks * 16)} B est.`,
      ms: compressed.expanded_ms ?? 0,
    });
  }
  const maxMs = Math.max(...rows.map((r) => r.ms), 0.001);
  $("cost-rows").innerHTML = rows
    .map(
      (row) => `<tr>
      <td>${row.label}</td>
      <td class="num">${fmt.format(row.segments)}</td>
      <td class="num">${row.ticks}</td>
      <td class="num">${row.bytes}</td>
      <td class="num">${row.ms.toFixed(2)} ms
        <span class="microbar"><span style="width:${Math.max(4, (row.ms / maxMs) * 100)}%"></span></span>
      </td>
    </tr>`
    )
    .join("");
  $("cost-note").textContent =
    `This window: ${fmt.format(compressed.segments_scanned)} compressed runs vs ` +
    `${fmt.format(expandedTicks)} expanded rows. Compressed cost tracks runs + buckets, ` +
    `not window width: widen the window with the same bucket width and it grows with the ` +
    `bucket count; widen the bucket width and it barely moves. Expanded cost always grows ` +
    `with the tick count. Answers are identical — see the pointwise check.`;
  const match = data.matches_expanded;
  $("cost-match").innerHTML =
    expandedTicks
      ? match
        ? `<span class="ok">✓ both paths agree</span>`
        : `<span class="bad">✗ paths disagree</span>`
      : `<span class="muted">expanded path not requested</span>`;
}

/* ---------- editor ---------- */

function editorValues() {
  const mode = document.querySelector('input[name="span-mode"]:checked')?.value || "ticks";
  if (mode === "clock") {
    const start = $("clock-start").value;
    const end = $("clock-end").value;
    if (!start || !end) return null;
    return { start: isoToTick(start), end: isoToTick(end) };
  }
  const start = Number($("start-tick").value);
  const end = Number($("end-tick").value);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  return { start, end };
}

function toggleSpanMode() {
  const mode = document.querySelector('input[name="span-mode"]:checked')?.value || "ticks";
  $("ticks-fields").hidden = mode !== "ticks";
  $("clock-fields").hidden = mode !== "clock";
  const values = editorValues();
  if (mode === "clock" && values) {
    $("clock-start").value = tickToLabel(values.start);
    $("clock-end").value = tickToLabel(values.end);
  }
}

function syncEditorToTicks() {
  const mode = document.querySelector('input[name="span-mode"]:checked')?.value || "ticks";
  if (mode === "clock") {
    const values = editorValues();
    if (values) {
      $("start-tick").value = values.start;
      $("end-tick").value = values.end;
    }
  } else {
    const values = editorValues();
    if (values) {
      $("clock-start").value = tickToLabel(values.start);
      $("clock-end").value = tickToLabel(values.end);
    }
  }
}

async function preview(options = {}) {
  const attempt = options.attempt || 0;
  const values = editorValues();
  const value = Number($("rev-value").value);
  if (!values) {
    toast("Enter both ends of the span.", "error");
    return;
  }
  if (!(values.end > values.start)) {
    toast("The interval is half-open: start must be strictly less than end.", "error");
    return;
  }
  if (!(value >= 0)) {
    toast("Value must be a non-negative integer.", "error");
    return;
  }
  $("preview-status").textContent = "previewing…";
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/preview`, {
      method: "POST",
      body: JSON.stringify({
        start: values.start,
        end: values.end,
        value,
        base_seq: state.head,
      }),
    });
    state.verify = null;
    renderImpact(body);
    $("preview-status").textContent = `preview against head ${body.head_seq}`;
    if (!options.silent) saveDraft({ quiet: true });
  } catch (error) {
    $("preview-status").textContent = "";
    if (error.code === "stale_preview") {
      if (attempt >= 2) {
        toast("The ledger keeps advancing; try again in a moment.", "error");
        return;
      }
      toast("The ledger advanced. Refreshing; your span is kept.", "error");
      await loadSources(false);
      await loadState();
      return preview({ silent: options.silent, attempt: attempt + 1 });
    }
    toast(error.message, "error");
  }
}

function renderImpact(preview) {
  state.impactPreview = preview;
  const host = $("impact");
  const residues = preview.residues;
  const residueCount = residues.left.length + residues.right.length;
  const unchanged = preview.span_ticks - preview.coverage_before;
  host.innerHTML = `
    <div class="impact-grid">
      <div class="impact-stat"><span class="label">span</span><span class="value">${ticks(preview.span_ticks)}</span><span class="sub">${spanLabel(preview.proposed.start, preview.proposed.end)}</span></div>
      <div class="impact-stat"><span class="label">was covered</span><span class="value">${ticks(preview.coverage_before)}</span><span class="sub">${unchanged > 0 ? `${ticks(unchanged)} uncovered ticks will be filled` : "no gap filling"}</span></div>
      <div class="impact-stat"><span class="label">integral</span><span class="value">${fmt.format(preview.old_integral)} → ${fmt.format(preview.new_integral)}</span><span class="sub">Δ ${signed(preview.new_integral - preview.old_integral)}</span></div>
      <div class="impact-stat"><span class="label">residues kept</span><span class="value">${residueCount}</span><span class="sub">${residueDesc(residues)}</span></div>
    </div>
    <div class="residue-rows">
      ${residues.left.map((s) => residueRow("left", s)).join("")}
      ${residues.right.map((s) => residueRow("right", s)).join("")}
      ${preview.replaced.map((s) => residueRow("replaced", s)).join("")}
    </div>
    <div class="split-basis">
      <h4>Changed ticks, from the ledger preview</h4>
      ${preview.diff.changed.length
        ? `<table class="mini"><thead><tr><th>interval</th><th>before</th><th>after</th><th>ticks</th></tr></thead><tbody>${preview.diff.changed
            .map(
              (row) => `<tr><td class="mono">[${row.start}, ${row.end})</td>
              <td>${row.before ? `value ${row.before.value} · #${row.before.source_entry}` : "uncovered"}</td>
              <td>value ${row.after.value} · #${row.after.source_entry}</td>
              <td class="num">${row.ticks}</td></tr>`
            )
            .join("")}</tbody></table>`
        : `<p class="muted">No value changes; the span already read ${preview.proposed.value}.</p>`}
      ${preview.diff.provenance_only.length
        ? `<p class="muted small">${preview.diff.provenance_only.length} further interval(s) keep their value but change owning entry.</p>`
        : ""}
    </div>`;
  $("commit-button").disabled = false;
  $("discard-button").disabled = false;
  $("impact-empty").hidden = true;
}

function signed(n) {
  return `${n >= 0 ? "+" : "−"}${fmt.format(Math.abs(n))}`;
}

function residueRow(kind, seg) {
  const label = kind === "left" ? "left residue" : kind === "right" ? "right residue" : "replaced";
  return `<div class="residue-row ${kind}">
    <span class="residue-kind">${label}</span>
    <span class="mono">${spanLabel(seg.start, seg.end)}</span>
    <span>value ${seg.value}</span>
    <span class="muted">${ticks(seg.ticks)} kept from #${seg.source_entry}</span>
  </div>`;
}

function residueDesc(residues) {
  const parts = [];
  if (residues.left.length) parts.push(`${ticks(residues.left.reduce((a, s) => a + s.ticks, 0))} left`);
  if (residues.right.length) parts.push(`${ticks(residues.right.reduce((a, s) => a + s.ticks, 0))} right`);
  return parts.length ? parts.join(" · ") : "whole affected runs replaced";
}

/* ---------- drafts ---------- */

function collectDraft() {
  const values = editorValues();
  return {
    start: values?.start ?? null,
    end: values?.end ?? null,
    value: Number($("rev-value").value),
    note: $("rev-note").value,
    base_seq: state.head,
  };
}

async function saveDraft({ quiet = false } = {}) {
  if (state.sourceId == null) return;
  const draft = collectDraft();
  if (draft.start == null || draft.end == null) return;
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/draft`, {
      method: "PUT",
      body: JSON.stringify(draft),
    });
    state.draftSeq = body.draft.updated_at;
    state.localDraft = draft;
    state.draftDirty = false;
    if (!quiet) toast("Draft saved on the merger service.");
    renderDraftState();
  } catch (error) {
    toast(`Draft not saved: ${error.message}`, "error");
  }
}

async function discardDraft() {
  await api(`/api/merger/sources/${state.sourceId}/draft`, { method: "DELETE" });
  state.localDraft = null;
  state.draftSeq = null;
  state.draftDirty = false;
  state.impactPreview = null;
  $("impact-empty").hidden = false;
  $("impact").innerHTML = "";
  $("rev-note").value = "";
  $("preview-status").textContent = "";
  $("commit-button").disabled = true;
  $("discard-button").disabled = true;
  renderDraftState();
  toast("Draft discarded.");
}

async function restoreDraft() {
  if (state.sourceId == null) return;
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/draft`);
    const draft = body.draft;
    if (draft.start != null) $("start-tick").value = draft.start;
    if (draft.end != null) $("end-tick").value = draft.end;
    if (draft.start != null) $("clock-start").value = tickToLabel(draft.start);
    if (draft.end != null) $("clock-end").value = tickToLabel(draft.end);
    if (draft.value != null) $("rev-value").value = draft.value;
    $("rev-note").value = draft.note || "";
    state.localDraft = draft;
    state.draftSeq = draft.updated_at;
    state.draftDirty = false;
    if (draft.start != null && draft.end != null) {
      await preview({ silent: true });
    }
    toast(`Draft restored (saved ${draft.updated_at}).`);
    renderDraftState();
  } catch (error) {
    if (error.status !== 404) toast(error.message, "error");
    renderDraftState();
  }
}

function renderDraftState() {
  const pill = $("draft-pill");
  if (state.localDraft && state.localDraft.start != null) {
    pill.hidden = false;
    pill.textContent = state.draftDirty ? "draft · unsaved edits" : "draft saved on merger";
    pill.dataset.state = state.draftDirty ? "dirty" : "saved";
  } else {
    pill.hidden = true;
  }
  const banner = $("restore-banner");
  if (banner) {
    const hasDraft = Boolean(state.localDraft && state.localDraft.start != null);
    banner.hidden = !hasDraft;
    if (hasDraft) {
      $("restore-text").textContent = state.draftDirty
        ? `Draft in this browser, not yet saved to the merger (${spanLabel(state.localDraft.start, state.localDraft.end)}).`
        : `Draft restored from the merger, saved ${state.localDraft.updated_at || state.draftSeq}.`;
    }
  }
}

/* ---------- commit / revoke / audit ---------- */

function clientId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

async function commit() {
  const values = editorValues();
  const value = Number($("rev-value").value);
  if (!values || !(values.end > values.start) || !(value >= 0)) {
    toast("Fix the span and value before committing.", "error");
    return;
  }
  const preview = state.impactPreview;
  if (preview && preview.head_seq !== state.head) {
    toast("The ledger changed after the preview. Preview again.", "error");
    return;
  }
  $("commit-button").disabled = true;
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/commit`, {
      method: "POST",
      body: JSON.stringify({
        start: values.start,
        end: values.end,
        value,
        note: $("rev-note").value,
        base_seq: state.head,
        client_id: clientId("ui-rev"),
      }),
    });
    state.localDraft = null;
    state.draftSeq = null;
    state.impactPreview = null;
    $("impact").innerHTML = "";
    $("impact-empty").hidden = false;
    $("preview-status").textContent = "";
    toast(`Revision #${body.entry.seq} committed. ${body.created ? "" : "Already present."}`);
    await loadSources(false);
    await loadState();
    await audit(body.entry.seq);
  } catch (error) {
    if (error.code === "stale_preview") {
      toast("Someone wrote ahead of this preview. Refreshing the ledger.", "error");
      await loadState();
    } else {
      toast(error.message, "error");
      $("commit-button").disabled = false;
    }
  }
}

async function revoke(seq) {
  if (!confirm(`Revoke revision #${seq}? Replay will restore the state as if it had never been applied.`)) return;
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/revoke?seq=${seq}`, {
      method: "POST",
      body: JSON.stringify({ target_seq: seq, client_id: clientId("ui-revoke") }),
    });
    toast(`Revision #${seq} revoked by entry #${body.entry.seq}.`);
    await loadSources(false);
    await loadState();
    await audit(seq);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function audit(seq) {
  state.auditSeq = seq;
  try {
    const body = await api(`/api/merger/sources/${state.sourceId}/audit?seq=${seq}`);
    state.audit = body;
    renderAudit(body);
    renderSegments(state.data);
    renderLedger(state.data);
  } catch (error) {
    if (error.status === 404) {
      toast("That entry is no longer in the ledger view.", "error");
    } else {
      toast(error.message, "error");
    }
  }
}

function renderAudit(body) {
  const panel = $("audit-panel");
  panel.hidden = false;
  const residues = body.residues;
  panel.innerHTML = `
    <header class="panel-head">
      <div>
        <h3>Split basis · entry #${body.entry.seq}</h3>
        <p class="muted">${body.entry.kind}${body.entry.start != null ? ` · ${spanLabel(body.entry.start, body.entry.end)} = ${body.entry.value}` : ""}${body.entry.note ? ` · “${escapeHtml(body.entry.note)}”` : ""}</p>
      </div>
      <button class="ghost" id="audit-close">Close</button>
    </header>
    ${body.revoked ? `<p class="warn-note">This revision is revoked; the state shown is what would apply with it restored.</p>` : ""}
    <div class="audit-columns">
      <div>
        <h4>Run residues</h4>
        <div class="residue-rows">
          ${residues.left.map((s) => residueRow("left", s)).join("") || `<p class="muted small">No left residue: the revision starts on an old boundary.</p>`}
          ${residues.right.map((s) => residueRow("right", s)).join("") || `<p class="muted small">No right residue: the revision ends on an old boundary.</p>`}
        </div>
      </div>
      <div>
        <h4>Replaced inside the span</h4>
        <div class="residue-rows">
          ${body.replaced.map((s) => residueRow("replaced", s)).join("") || `<p class="muted small">The span was uncovered before writing.</p>`}
        </div>
      </div>
    </div>
    <div class="audit-diff">
      <h4>Tick-level change</h4>
      ${body.diff.changed.length
        ? `<table class="mini"><thead><tr><th>interval</th><th>before</th><th>after</th><th>ticks</th></tr></thead><tbody>${body.diff.changed
            .map(
              (row) => `<tr><td class="mono">[${row.start}, ${row.end})</td>
              <td>${row.before ? `value ${row.before.value} · #${row.before.source_entry}` : "uncovered"}</td>
              <td>${row.after ? `value ${row.after.value} · #${row.after.source_entry}` : "uncovered"}</td>
              <td class="num">${row.ticks}</td></tr>`
            )
            .join("")}</tbody></table>`
        : `<p class="muted">This entry produces no value change.</p>`}
      <p class="muted small">∫ ${fmt.format(body.diff.integral_before)} → ${fmt.format(body.diff.integral_after)} on ${spanLabel(body.span.lo, body.span.hi)}</p>
    </div>`;
  $("audit-close").addEventListener("click", () => {
    panel.hidden = true;
    state.auditSeq = null;
    renderSegments(state.data);
    renderLedger(state.data);
  });
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadIntoEditor(seq) {
  const body = await api(`/api/merger/sources/${state.sourceId}/state?${new URLSearchParams({
    lo: state.lo,
    hi: state.hi,
    granularity: state.granularity,
  })}`);
  const entry = body.entries.find((e) => e.seq === seq);
  if (!entry) return;
  if (entry.kind === "observation") {
    const first = entry.runs[0];
    const last = entry.runs[entry.runs.length - 1];
    $("start-tick").value = first.start;
    $("end-tick").value = last.end;
    $("rev-value").value = entry.runs[0].value;
  } else {
    $("start-tick").value = entry.start;
    $("end-tick").value = entry.end;
    $("rev-value").value = entry.value;
  }
  $("clock-start").value = tickToLabel(Number($("start-tick").value));
  $("clock-end").value = tickToLabel(Number($("end-tick").value));
  $("rev-note").value = entry.note || "";
  state.draftDirty = true;
  renderDraftState();
  await preview({ silent: true });
}

/* ---------- pointwise verification ---------- */

async function verify() {
  const lo = Math.max(0, state.lo);
  const hi = Math.min(state.hi, lo + 240);
  $("verify-result").innerHTML = `<span class="muted">Expanding ${hi - lo} ticks…</span>`;
  try {
    const body = await api(
      `/api/query/sources/${state.sourceId}/verify?lo=${lo}&hi=${hi}&granularity=${state.granularity}`
    );
    const ok = body.ok;
    $("verify-result").innerHTML = `
      <div class="verify-head ${ok ? "ok" : "bad"}">${ok ? "✓ compressed aggregate matches the pointwise baseline" : "✗ mismatch found"}</div>
      <p class="muted small">${fmt.format(body.point_rows)} point rows expanded into ${body.compressed_buckets} buckets on ${spanLabel(body.lo, body.hi)} · ${body.head_seq} ledger entries replayed</p>
      ${
        body.mismatches.length
          ? `<table class="mini"><thead><tr><th>bucket</th><th>compressed</th><th>pointwise</th></tr></thead><tbody>${body.mismatches
              .map((m) => `<tr><td class="mono">[${m.start}, ${m.end})</td><td>${m.compressed}</td><td>${m.pointwise}</td></tr>`)
              .join("")}</tbody></table>`
          : ""
      }
      <details><summary>Edge checks (half-open at both ends)</summary>
        <table class="mini"><thead><tr><th>tick</th><th>value</th><th>present</th></tr></thead><tbody>
        ${body.edge_checks
          .map((e) => `<tr><td class="num">${e.tick}</td><td class="num">${e.value ?? "—"}</td><td>${e.expected_present ? "yes" : "no"}</td></tr>`)
          .join("")}
        </tbody></table>
      </details>`;
  } catch (error) {
    $("verify-result").innerHTML = `<span class="bad">Verification failed: ${escapeHtml(error.message)}</span>`;
  }
}

/* ---------- wiring ---------- */

function bind() {
  $("source-select").addEventListener("change", async (event) => {
    state.sourceId = Number(event.target.value);
    state.auditSeq = null;
    $("audit-panel").hidden = true;
    await resetWindowToExtent();
    await loadState();
    await restoreDraft();
  });

  $("lo-input").addEventListener("change", () => applyWindow());
  $("hi-input").addEventListener("change", () => applyWindow());
  $("granularity").addEventListener("change", () => applyWindow());
  $("revoked-toggle").addEventListener("change", () => loadState());

  $("apply-window").addEventListener("click", () => applyWindow());
  $("preset-2h").addEventListener("click", () => presetWindow(120));
  $("preset-4h").addEventListener("click", () => presetWindow(240));
  $("preset-all").addEventListener("click", () => presetWindow(null));

  for (const input of document.querySelectorAll('input[name="span-mode"]')) {
    input.addEventListener("change", toggleSpanMode);
  }
  for (const id of ["start-tick", "end-tick", "clock-start", "clock-end", "rev-value", "rev-note"]) {
    $(id).addEventListener("input", () => {
      state.draftDirty = true;
      renderDraftState();
      if (id.endsWith("tick") || id.startsWith("clock")) syncEditorToTicks();
      clearTimeout(state.previewTimer);
      state.previewTimer = setTimeout(() => {
        if (editorValues() && $("rev-value").value !== "") preview({ silent: true });
      }, 450);
      clearTimeout(state.autosaveTimer);
      state.autosaveTimer = setTimeout(async () => {
        const values = editorValues();
        if (values && values.end > values.start) await saveDraft({ quiet: true });
      }, 900);
    });
  }

  $("preview-button").addEventListener("click", () => preview());
  $("banner-discard").addEventListener("click", discardDraft);
  $("commit-button").addEventListener("click", commit);
  $("discard-button").addEventListener("click", discardDraft);
  $("verify-button").addEventListener("click", verify);
  $("restore-draft").addEventListener("click", restoreDraft);

  window.addEventListener("resize", debounce(() => state.data && renderChart(state.data), 150));
}

async function resetWindowToExtent() {
  const probe = await api(`/api/merger/sources/${state.sourceId}/state?lo=0&hi=1&granularity=1`);
  const extent = probe.extent || {};
  const lo = extent.extent_start ?? 0;
  const end = extent.extent_end ?? lo + 60;
  state.lo = lo;
  // Default to the first 8 hours of a longer source; the full extent otherwise.
  state.hi = end - lo > 480 ? lo + 480 : end;
  $("lo-input").value = state.lo;
  $("hi-input").value = state.hi;
}

function applyWindow() {
  const lo = Math.max(0, Number($("lo-input").value) || 0);
  const hi = Math.max(lo + 1, Number($("hi-input").value) || lo + 60);
  state.lo = lo;
  state.hi = hi;
  state.granularity = Number($("granularity").value);
  loadState();
}

function presetWindow(span) {
  if (span == null) {
    const extent = state.data?.extent;
    state.lo = extent?.extent_start ?? 0;
    state.hi = extent?.extent_end ?? 480;
  } else {
    const lo = Number($("lo-input").value) || 0;
    state.lo = lo;
    state.hi = lo + span;
  }
  $("lo-input").value = state.lo;
  $("hi-input").value = state.hi;
  loadState();
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

async function boot() {
  bind();
  try {
    const health = await api("/api/query/health");
    state.epoch = health.epoch || state.epoch;
  } catch {
    /* the query service also serves this page; health should not fail */
  }
  await loadSources();
  if (state.sourceId != null) {
    await resetWindowToExtent();
    await loadState();
    await restoreDraft();
  }
}

document.addEventListener("DOMContentLoaded", boot);
