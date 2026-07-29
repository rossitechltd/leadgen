(function () {
  const POLL_IDLE_MS = 8000;
  const POLL_RUNNING_MS = 1000;
  const POLL_PIPELINE_MS = 3000;

  const els = {
    btnRunAll: document.getElementById("btn-run-all"),
    btnAbandonRun: document.getElementById("btn-abandon-run"),
    pipelineBadge: document.getElementById("pipeline-badge"),
    pipelineProgressWrap: document.getElementById("pipeline-progress-wrap"),
    pipelineProgressBar: document.getElementById("pipeline-progress-bar"),
    pipelineProgressPct: document.getElementById("pipeline-progress-pct"),
    pipelineProgressLabel: document.getElementById("pipeline-progress-label"),
    pipelineProgressEta: document.getElementById("pipeline-progress-eta"),
    pipelineStepStrip: document.getElementById("pipeline-step-strip"),
    pipelineStepChips: document.querySelectorAll(".pipeline-step-chip"),
    pipelineOutcome: document.getElementById("pipeline-outcome"),
    pipelineOutcomeTitle: document.getElementById("pipeline-outcome-title"),
    pipelineOutcomeDetail: document.getElementById("pipeline-outcome-detail"),
    pipelineOutcomeStats: document.getElementById("pipeline-outcome-stats"),
    btnRunAllSpinner: document.querySelector(".btn-run-all-spinner"),
    cfgSchedule: document.getElementById("cfg-schedule"),
    cfgScheduler: document.getElementById("cfg-scheduler"),
    cfgNextRun: document.getElementById("cfg-next-run"),
    cfgLastRun: document.getElementById("cfg-last-run"),
    cfgSheets: document.getElementById("cfg-sheets"),
    cfgDynamic: document.getElementById("cfg-dynamic"),
    cfgImported: document.getElementById("cfg-imported"),
    cfgReady: document.getElementById("cfg-ready"),
    cfgTelegram: document.getElementById("cfg-telegram"),
    attentionPanel: document.getElementById("attention-panel"),
    attentionList: document.getElementById("attention-list"),
    btnResumeStep1: document.getElementById("btn-resume-step1"),
    leadsUploadInput: document.getElementById("leads-upload-input"),
    leadsFileInput: document.getElementById("leads-file-input"),
    btnUploadLeads: document.getElementById("btn-upload-leads"),
    leadsUploadMessage: document.getElementById("leads-upload-message"),
    leadsUploadModal: document.getElementById("leads-upload-modal"),
    modalLeadsInput: document.getElementById("modal-leads-input"),
    modalLeadsFileInput: document.getElementById("modal-leads-file-input"),
    modalLeadsMessage: document.getElementById("modal-leads-message"),
    leadsUploadCancel: document.getElementById("leads-upload-cancel"),
    leadsUploadConfirm: document.getElementById("leads-upload-confirm"),
    healthStatus: document.getElementById("health-status"),
    logPanel: document.getElementById("log-panel"),
    btnClearLogs: document.getElementById("btn-clear-logs"),
    stepCards: document.querySelectorAll(".step-card"),
    runStepBtns: document.querySelectorAll(".btn-run-step"),
    notificationStack: document.getElementById("notification-stack"),
  };

  const NOTIFICATION_TTL_MS = 9000;
  const MAX_NOTIFICATIONS = 6;
  const TERMINAL_STEP_STATUSES = ["success", "failed", "skipped", "waiting", "abandoned"];

  let localLogView = [];
  let pollTimer = null;
  let progressTick = null;
  let activeRunStepIds = [];
  let lastPipelineSnapshot = null;
  let clientRunPending = false;
  let runAllActive = false;
  let notifiedKeys = new Set();
  let lastPipelineStartedAt = null;
  let lastRunningStepId = null;
  let notificationsSeeded = false;
  let leadsUploadModalResolver = null;
  let leadsUploadModalMode = "step1";
  let lastTimingConfig = null;

  function showNotification(title, detail, variant = "info") {
    if (!els.notificationStack) {
      return;
    }

    const icons = {
      info: "i",
      success: "✓",
      warning: "!",
      error: "×",
      waiting: "…",
    };

    const toast = document.createElement("div");
    toast.className = "notification notification-" + variant;
    toast.innerHTML =
      '<span class="notification-icon" aria-hidden="true">' +
      (icons[variant] || icons.info) +
      "</span>" +
      '<div class="notification-body">' +
      '<div class="notification-title">' +
      escapeHtml(title) +
      "</div>" +
      (detail ? '<div class="notification-detail">' + escapeHtml(detail) + "</div>" : "") +
      "</div>";

    const existing = els.notificationStack.querySelectorAll(".notification");
    if (existing.length >= MAX_NOTIFICATIONS) {
      dismissNotification(existing[0]);
    }

    els.notificationStack.appendChild(toast);

    const ttl = variant === "error" ? NOTIFICATION_TTL_MS * 1.5 : NOTIFICATION_TTL_MS;
    const timer = setTimeout(() => dismissNotification(toast), ttl);
    toast._dismissTimer = timer;
  }

  function dismissNotification(el) {
    if (!el || el.classList.contains("is-leaving")) {
      return;
    }
    if (el._dismissTimer) {
      clearTimeout(el._dismissTimer);
    }
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 280);
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function stepStatusVariant(status) {
    if (status === "success") return "success";
    if (status === "failed") return "error";
    if (status === "waiting") return "waiting";
    if (status === "skipped") return "warning";
    if (status === "abandoned") return "warning";
    return "info";
  }

  function outcomeVariant(outcome) {
    if (outcome === "completed_all") return "success";
    if (outcome === "failed") return "error";
    if (outcome === "waiting") return "waiting";
    if (outcome === "no_work" || outcome === "stopped" || outcome === "abandoned") return "warning";
    return "info";
  }

  function formatStepNotification(step) {
    const stats = step.stats || {};
    const name = step.name || "Step " + step.id;

    if (step.id === 1) {
      const appended = stats.rows_appended || 0;
      const parsed = stats.parsed || 0;
      if (step.status === "success" && appended > 0) {
        return {
          title: appended + " lead" + (appended === 1 ? "" : "s") + " imported",
          detail: parsed > appended ? parsed + " parsed, " + appended + " new" : step.message || "",
        };
      }
      if (step.status === "skipped") {
        if (stats.duplicate_dynamic || stats.duplicate_imported) {
          return {
            title: "No new leads imported",
            detail:
              (stats.duplicate_dynamic || 0) +
              " already on sheet, " +
              (stats.duplicate_imported || 0) +
              " in allimported",
          };
        }
        return { title: "No leads to import", detail: step.message || "" };
      }
      if (step.status === "failed") {
        return { title: "Lead import failed", detail: step.message || "" };
      }
    }

    if (step.id === 2) {
      const removed = stats.removed || 0;
      const kept = stats.kept || 0;
      if (removed > 0) {
        return {
          title: removed + " duplicate" + (removed === 1 ? "" : "s") + " removed",
          detail: kept + " lead" + (kept === 1 ? "" : "s") + " remain on sheet",
        };
      }
      return {
        title: "No duplicates found",
        detail: kept + " lead" + (kept === 1 ? "" : "s") + " on Dynamic Lead Sheet",
      };
    }

    if (step.id === 3) {
      const removed = (stats.removed_heuristic || 0) + (stats.removed_ai || 0);
      const screened = stats.screened || 0;
      if (removed > 0) {
        return {
          title: removed + " personal profile" + (removed === 1 ? "" : "s") + " removed",
          detail:
            screened +
            " screened — " +
            (stats.tagged_business || 0) +
            " business, " +
            (stats.tagged_uncertain || 0) +
            " uncertain",
        };
      }
      if (step.status === "success") {
        return {
          title: "Entity screen done",
          detail:
            screened +
            " screened — " +
            (stats.tagged_business || 0) +
            " business tagged",
        };
      }
      if (step.status === "skipped") {
        return { title: "Nothing to screen", detail: step.message || "" };
      }
      if (step.status === "failed") {
        return { title: "Entity screen failed", detail: step.message || "" };
      }
    }

    if (step.id === 4) {
      const scraped = stats.scraped_count || 0;
      const total = stats.total_to_scrape || 0;
      const failed = stats.failed_count || 0;
      if (step.status === "success" && scraped > 0) {
        return {
          title:
            scraped +
            " page" +
            (scraped === 1 ? "" : "s") +
            " scraped",
          detail:
            total
              ? scraped + "/" + total + " leads" + (failed ? ", " + failed + " failed" : "")
              : step.message || "Scrape queue updated",
        };
      }
      if (step.status === "success") {
        return {
          title: "Page scraped",
          detail: step.message || "Scrape queue updated",
        };
      }
      if (step.status === "waiting") {
        return {
          title: "Still waiting on page scrape",
          detail: step.message || "MMM scrape not finished yet",
        };
      }
      if (step.status === "skipped") {
        return { title: "No pages to scrape", detail: step.message || "" };
      }
      if (step.status === "failed") {
        return { title: "Page scrape failed", detail: step.message || "" };
      }
    }

    if (step.id === 5) {
      const refined = stats.updated || stats.processed || 0;
      if (refined > 0) {
        return {
          title: refined + " profile" + (refined === 1 ? "" : "s") + " refined",
          detail: "Structured fields extracted from scrape text",
        };
      }
      return { title: "Nothing to refine", detail: step.message || "" };
    }

    if (step.id === 6) {
      const tagged = stats.tagged_business || 0;
      const removed = (stats.removed_heuristic || 0) + (stats.removed_ai || 0);
      const still = stats.still_uncertain || 0;
      if (tagged > 0 || removed > 0) {
        const parts = [];
        if (tagged) parts.push(tagged + " tagged business");
        if (removed) parts.push(removed + " personal removed");
        if (still) parts.push(still + " still uncertain");
        return { title: "Entity clarify done", detail: parts.join(", ") };
      }
      if (step.status === "skipped") {
        return { title: "No uncertain leads", detail: step.message || "" };
      }
      if (step.status === "failed") {
        return { title: "Entity clarify failed", detail: step.message || "" };
      }
    }

    if (step.id === 7) {
      const kept = stats.kept || 0;
      const removed = stats.removed || 0;
      if (kept > 0 || removed > 0) {
        const parts = [];
        if (kept) parts.push(kept + " qualified");
        if (removed) parts.push(removed + " removed");
        return { title: "AI qualify done", detail: parts.join(", ") };
      }
      return { title: "Nothing to qualify", detail: step.message || "" };
    }

    if (step.id === 8) {
      const moved = stats.moved || 0;
      const sheet = stats.destination_sheet;
      if (moved > 0) {
        return {
          title: moved + " lead" + (moved === 1 ? "" : "s") + " finalised",
          detail: sheet ? "Moved to " + sheet : step.message || "",
        };
      }
      return { title: "Nothing to finalise", detail: step.message || "" };
    }

    return { title: name + " finished", detail: step.message || step.status };
  }

  function seedNotificationKeys(pipeline, steps) {
    if (pipeline.started_at) {
      lastPipelineStartedAt = pipeline.started_at;
      notifiedKeys.add("start:" + pipeline.started_at);
    }
    steps.forEach((step) => {
      if (TERMINAL_STEP_STATUSES.includes(step.status) && step.finished_at) {
        notifiedKeys.add("step:" + step.id + ":" + step.finished_at);
      }
    });
    if (!pipeline.is_running && pipeline.finished_at && pipeline.outcome) {
      notifiedKeys.add("outcome:" + pipeline.finished_at);
    }
  }

  function resetNotificationTracking() {
    notifiedKeys = new Set();
    lastRunningStepId = null;
  }

  function processNotifications(pipeline, steps) {
    if (pipeline.started_at && pipeline.started_at !== lastPipelineStartedAt) {
      lastPipelineStartedAt = pipeline.started_at;
      resetNotificationTracking();
      const key = "start:" + pipeline.started_at;
      if (!notifiedKeys.has(key)) {
        notifiedKeys.add(key);
        const stepCount = pipeline.running_step_ids?.length || steps.length;
        showNotification(
          "Pipeline started",
          stepCount > 1 ? "Running " + stepCount + " steps" : "Running step",
          "info"
        );
      }
    }

    if (pipeline.is_running && pipeline.current_step_id !== lastRunningStepId) {
      const step = steps.find((s) => s.id === pipeline.current_step_id);
      if (step && step.status === "running") {
        const key = "running:" + pipeline.started_at + ":" + step.id;
        if (!notifiedKeys.has(key)) {
          notifiedKeys.add(key);
          showNotification("Running " + step.name, "Step " + step.id, "info");
        }
        lastRunningStepId = pipeline.current_step_id;
      }
    }

    steps.forEach((step) => {
      if (!TERMINAL_STEP_STATUSES.includes(step.status) || !step.finished_at) {
        return;
      }
      const key = "step:" + step.id + ":" + step.finished_at;
      if (notifiedKeys.has(key)) {
        return;
      }
      notifiedKeys.add(key);
      const { title, detail } = formatStepNotification(step);
      showNotification(title, detail, stepStatusVariant(step.status));
    });

    if (!pipeline.is_running && pipeline.finished_at && pipeline.outcome) {
      const key = "outcome:" + pipeline.finished_at;
      if (!notifiedKeys.has(key)) {
        notifiedKeys.add(key);
        const title = OUTCOME_LABELS[pipeline.outcome] || "Pipeline finished";
        showNotification(title, pipeline.outcome_message || "", outcomeVariant(pipeline.outcome));
      }
    }
  }

  function formatTime(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  function parseIso(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function formatDuration(secs) {
    if (secs <= 0) return "0s";
    if (secs < 60) return Math.ceil(secs) + "s";
    const m = Math.floor(secs / 60);
    const s = Math.ceil(secs % 60);
    return s > 0 ? m + "m " + s + "s" : m + "m";
  }

  function setBadge(el, status) {
    el.textContent = status;
    el.className = "badge badge-" + status;
  }

  function setProgressBar(bar, pct, variant) {
    const clamped = Math.max(0, Math.min(100, pct));
    bar.style.width = clamped + "%";
    bar.classList.remove("is-complete", "is-failed", "is-skipped", "is-waiting");
    if (variant === "complete") bar.classList.add("is-complete");
    if (variant === "failed") bar.classList.add("is-failed");
    if (variant === "skipped") bar.classList.add("is-skipped");
    if (variant === "waiting") bar.classList.add("is-waiting");
  }

  const OUTCOME_LABELS = {
    completed_all: "All steps completed",
    partial: "Partial run",
    no_work: "No work performed",
    stopped: "Pipeline stopped early",
    failed: "Pipeline failed",
    waiting: "Pipeline waiting",
    abandoned: "Pipeline abandoned",
    unknown: "Run finished",
  };

  function updatePipelineOutcome(pipeline) {
    if (!els.pipelineOutcome) {
      return;
    }

    if (pipeline.is_running || !pipeline.outcome) {
      els.pipelineOutcome.hidden = true;
      els.pipelineOutcome.className = "pipeline-outcome";
      return;
    }

    const outcome = pipeline.outcome || "unknown";
    els.pipelineOutcome.hidden = false;
    els.pipelineOutcome.className = "pipeline-outcome outcome-" + outcome;
    els.pipelineOutcomeTitle.textContent =
      OUTCOME_LABELS[outcome] || OUTCOME_LABELS.unknown;
    els.pipelineOutcomeDetail.textContent =
      pipeline.outcome_message || "Last pipeline run finished.";

    const summary = pipeline.outcome_summary || {};
    const parts = [];
    if (summary.success) parts.push(summary.success + " completed");
    if (summary.skipped) parts.push(summary.skipped + " skipped");
    if (summary.failed) parts.push(summary.failed + " failed");
    if (summary.waiting) parts.push(summary.waiting + " waiting");
    if (summary.abandoned) parts.push(summary.abandoned + " abandoned");
    els.pipelineOutcomeStats.textContent = parts.length
      ? parts.join(" · ")
      : "";
  }

  function inferLeadCount(steps) {
    const stat = (id, key) => {
      const step = steps.find((s) => s.id === id);
      return step && step.stats ? Number(step.stats[key]) || 0 : 0;
    };

    if (stat(4, "total_to_scrape")) return stat(4, "total_to_scrape");
    if (stat(3, "total")) return stat(3, "total");
    if (stat(3, "screened")) return stat(3, "screened");
    if (stat(5, "total")) return stat(5, "total");
    if (stat(6, "total")) return stat(6, "total");
    if (stat(7, "total")) return stat(7, "total");
    if (stat(2, "kept")) return stat(2, "kept");
    if (stat(1, "rows_appended")) return stat(1, "rows_appended");
    return 0;
  }

  function stepDurationEstimate(step, nowMs, leadCount, timing) {
    const n = leadCount || 0;
    const batchSize = Number(timing?.entity_classify_batch_size) || 10;
    const perLeadScrape = Number(timing?.per_lead_scrape_secs) || 180;
    const qualifyPerRow =
      Math.max(
        Number(timing?.qualify_website_timeout_secs) || 15,
        12
      ) + 2;

    if (step.stats?.total > 0) {
      const total = Number(step.stats.total) || 0;
      const processed = Number(step.stats.processed) || 0;
      const perItem = Number(step.stats.per_item_estimate_secs) || 5;
      return Math.max(0, total - processed) * perItem;
    }

    switch (step.id) {
      case 1:
        return 20;
      case 2:
        return 15;
      case 3:
        return Math.ceil(n / batchSize) * 22 + n * 0.5 + 10;
      case 4:
        if (step.stats?.total_to_scrape) {
          const total = Number(step.stats.total_to_scrape) || 0;
          const done =
            (Number(step.stats.scraped_count) || 0) +
            (Number(step.stats.failed_count) || 0);
          const perLead =
            Number(step.stats.per_lead_estimate_secs) || perLeadScrape;
          const remaining = Math.max(0, total - done);
          if (step.status === "running" && remaining > 0) {
            const leadStarted = parseIso(step.stats.current_lead_started_at);
            const leadElapsed = leadStarted
              ? (nowMs - leadStarted.getTime()) / 1000
              : 0;
            return (remaining - 1) * perLead + Math.max(0, perLead - leadElapsed);
          }
          return remaining * perLead;
        }
        return n > 0 ? n * perLeadScrape : perLeadScrape;
      case 5:
        return n > 0 ? n * 10 : 60;
      case 6:
        return Math.ceil(n / batchSize) * 28 + n * 0.5 + 8;
      case 7:
        return n > 0 ? n * qualifyPerRow : qualifyPerRow * 3;
      case 8:
        return 25;
      default:
        return step.estimated_duration_secs || 60;
    }
  }

  function computePageScrapeProgress(step, nowMs) {
    const stats = step.stats || {};
    const total = Number(stats.total_to_scrape) || 0;
    const scraped = Number(stats.scraped_count) || 0;
    const failed = Number(stats.failed_count) || 0;
    const pasted = Number(stats.scrapesheet_pasted_count) || 0;
    const done = scraped + failed;
    const displayDone = Number(stats.display_done_count) || Math.max(done, pasted);
    const perLead =
      Number(stats.per_lead_estimate_secs) ||
      Number(lastTimingConfig?.per_lead_scrape_secs) ||
      180;
    const currentLead = Number(stats.current_lead) || Math.min(done + 1, total);

    const leadStarted = parseIso(stats.current_lead_started_at);
    const leadElapsed = leadStarted ? (nowMs - leadStarted.getTime()) / 1000 : 0;
    const leadFraction = Math.min(0.95, leadElapsed / perLead);

    const basePct = total > 0 ? (displayDone / total) * 100 : 0;
    const currentLeadPct = total > 0 ? (leadFraction / total) * 100 : 0;
    const pct = Math.min(95, Math.round(basePct + currentLeadPct));

    const remainingLeads = Math.max(0, total - done);
    const remainingSecs =
      remainingLeads * perLead + Math.max(0, perLead - leadElapsed);

    const pastedNote =
      pasted > scraped ? " (" + pasted + " pasted)" : "";

    return {
      pct,
      variant: null,
      etaText:
        remainingSecs > 0
          ? "~" +
            formatDuration(remainingSecs) +
            " left (" +
            done +
            "/" +
            total +
            " saved" +
            pastedNote +
            ")"
          : "Finishing up…",
      label:
        "Lead " +
        Math.min(currentLead, total) +
        " of " +
        total +
        " — " +
        pasted +
        " pasted, " +
        scraped +
        " saved" +
        (failed ? ", " + failed + " failed" : ""),
    };
  }

  function computeCountBasedProgress(step, nowMs) {
    const stats = step.stats || {};
    const total = Number(stats.total) || 0;
    const processed = Number(stats.processed) || Number(stats.done) || 0;
    if (total <= 0) {
      return null;
    }

    const perItem = Number(stats.per_item_estimate_secs) || 5;
    const pct = Math.min(95, Math.round((processed / total) * 100));
    const remainingSecs = Math.max(0, total - processed) * perItem;

    const stepLabels = {
      3: "Entity screen",
      5: "Refine",
      6: "Entity clarify",
      7: "Qualify",
    };
    const prefix = stepLabels[step.id] || "Processing";
    const currentRow = Number(stats.current_row) || 0;
    const position = Number(stats.position) || processed;
    const rowHint =
      currentRow > 0 ? " (sheet row " + currentRow + ")" : "";

    return {
      pct,
      variant: null,
      etaText:
        remainingSecs > 0
          ? "~" + formatDuration(remainingSecs) + " left (" + position + "/" + total + ")"
          : "Finishing up…",
      label: prefix + " — " + position + " of " + total + rowHint,
    };
  }

  function computeTimerStepProgress(step, nowMs, estimateSecs) {
    const estimate = estimateSecs || step.estimated_duration_secs || 60;

    if (step.status === "success" || step.status === "failed" || step.status === "skipped") {
      const variant =
        step.status === "failed"
          ? "failed"
          : step.status === "skipped"
            ? "skipped"
            : "complete";
      return {
        pct: 100,
        variant,
        etaText:
          step.status === "success"
            ? "Completed"
            : step.status === "skipped"
              ? "Skipped"
              : step.status,
        label:
          step.status === "skipped"
            ? step.message || "Skipped"
            : step.status === "running"
              ? "Running…"
              : "Done",
      };
    }

    if (step.status === "abandoned") {
      return {
        pct: 100,
        variant: "skipped",
        etaText: "Abandoned",
        label: step.message || "Abandoned by user",
      };
    }

    if (step.status === "waiting") {
      return {
        pct: 55,
        variant: "waiting",
        etaText: "Waiting",
        label: step.message || "Waiting for external action",
      };
    }

    if (step.status !== "running") {
      return { pct: 0, variant: null, etaText: "", label: "" };
    }

    const started = parseIso(step.started_at);
    const elapsed = started ? (nowMs - started.getTime()) / 1000 : 0;
    const pct = Math.min(95, Math.round((elapsed / estimate) * 100));
    const remaining = Math.max(0, estimate - elapsed);
    const etaText =
      remaining > 0
        ? "~" + formatDuration(remaining) + " left (est. " + formatDuration(estimate) + ")"
        : "Taking longer than expected…";

    return {
      pct,
      variant: null,
      etaText,
      label: "Running — predicted " + formatDuration(estimate),
    };
  }

  function step4BatchIncomplete(steps) {
    const step4 = steps.find((s) => s.id === 4);
    if (!step4 || !step4.stats || !step4.stats.total_to_scrape) {
      return false;
    }
    const total = Number(step4.stats.total_to_scrape) || 0;
    const done =
      (Number(step4.stats.scraped_count) || 0) +
      (Number(step4.stats.failed_count) || 0);
    return (
      total > 0 &&
      done < total &&
      (step4.status === "running" || step4.status === "waiting")
    );
  }

  /** Fast scrape-progress polls only while Step 4 is actually running or paused mid-batch. */
  function step4NeedsLiveUpdates(steps) {
    const step4 = steps.find((s) => s.id === 4);
    if (!step4) {
      return false;
    }
    if (step4.status === "running") {
      return true;
    }
    return step4.status === "waiting" && step4BatchIncomplete(steps);
  }

  function step4ProgressStats(step) {
    if (step.id !== 4) {
      return null;
    }
    if (step.stats?.total_to_scrape || step.stats?.manual_complete || step.stats?.awaiting_manual) {
      return step.stats;
    }
    return null;
  }

  function computeStepProgress(step, nowMs, leadCount, timing) {
    const step4Stats = step4ProgressStats(step);
    if (
      step.id === 4 &&
      step4Stats?.awaiting_manual &&
      (step.status === "running" || step.status === "waiting")
    ) {
      return {
        pct: 10,
        variant: "waiting",
        etaText: "",
        label: "Scrape manually, then mark completed in the modal",
      };
    }
    if (
      step.id === 4 &&
      step4Stats &&
      (step.status === "running" || step.status === "waiting")
    ) {
      return computePageScrapeProgress(
        { ...step, stats: step4Stats, status: "running" },
        nowMs
      );
    }

    const countProgress = computeCountBasedProgress(step, nowMs);
    if (countProgress && step.status === "running") {
      return countProgress;
    }

    const estimate = stepDurationEstimate(step, nowMs, leadCount, timing);
    return computeTimerStepProgress(step, nowMs, estimate);
  }

  function computePipelineProgress(pipeline, steps, nowMs) {
    const runSteps = steps.filter((s) => activeRunStepIds.includes(s.id));
    if (!runSteps.length) {
      return { pct: 0, etaText: "", label: "Pipeline running" };
    }

    const leadCount = inferLeadCount(steps);
    let totalEstimate = 0;
    let weighted = 0;

    for (const step of runSteps) {
      const est = stepDurationEstimate(step, nowMs, leadCount, lastTimingConfig);
      totalEstimate += est;
      if (
        step.status === "success" ||
        step.status === "failed" ||
        step.status === "skipped" ||
        step.status === "abandoned"
      ) {
        weighted += est;
      } else if (step.status === "running") {
        const prog = computeStepProgress(step, nowMs, leadCount, lastTimingConfig);
        weighted += (prog.pct / 100) * est;
      }
    }

    const pct = totalEstimate > 0 ? Math.round((weighted / totalEstimate) * 100) : 0;
    const current = steps.find((s) => s.id === pipeline.current_step_id);
    const currentName = current ? current.name : "Pipeline";

    let remaining = 0;
    const currentId = pipeline.current_step_id || 0;
    for (const step of runSteps) {
      if (step.status === "running") {
        remaining += stepDurationEstimate(step, nowMs, leadCount, lastTimingConfig);
      } else if (step.status === "idle" && step.id > currentId) {
        remaining += stepDurationEstimate(step, nowMs, leadCount, lastTimingConfig);
      }
    }

    const etaText =
      remaining > 0
        ? "~" +
          formatDuration(remaining) +
          " remaining (est. " +
          formatDuration(totalEstimate) +
          " for this run)"
        : "Finishing up…";

    return {
      pct,
      etaText,
      label: currentName + " — step " + pipeline.current_step_id,
    };
  }

  function isRunAllActive() {
    return runAllActive && activeRunStepIds.length > 1;
  }

  function updateRunAllButton(busy) {
    const showSpinner = busy && isRunAllActive();
    els.btnRunAll.classList.toggle("btn-run-all-busy", showSpinner);
    if (els.btnRunAllSpinner) {
      els.btnRunAllSpinner.hidden = !showSpinner;
    }
  }

  function updatePipelineStepStrip(steps) {
    if (!els.pipelineStepStrip) {
      return;
    }
    const showStrip = isRunAllActive() && (clientRunPending || lastPipelineSnapshot?.pipeline?.is_running);
    els.pipelineStepStrip.hidden = !showStrip;
    if (!showStrip) {
      return;
    }

    const currentId = lastPipelineSnapshot?.pipeline?.current_step_id || 0;
    els.pipelineStepChips.forEach((chip) => {
      const stepId = Number(chip.dataset.stripStep);
      const step = steps.find((s) => s.id === stepId);
      chip.classList.remove(
        "is-queued",
        "is-running",
        "is-success",
        "is-failed",
        "is-waiting",
        "is-skipped",
        "is-skipped-not-run"
      );

      if (!activeRunStepIds.includes(stepId)) {
        chip.hidden = true;
        return;
      }
      chip.hidden = false;

      if (!step || step.status === "idle" || step.status === "waiting") {
        if (clientRunPending && !lastPipelineSnapshot?.pipeline?.is_running) {
          chip.classList.add("is-queued");
        } else if (stepId > currentId) {
          chip.classList.add("is-queued");
        } else {
          chip.classList.add("is-queued");
        }
        return;
      }

      if (step.status === "running") {
        chip.classList.add("is-running");
      } else if (step.status === "success") {
        chip.classList.add("is-success");
      } else if (step.status === "failed") {
        chip.classList.add("is-failed");
      } else if (step.status === "waiting") {
        chip.classList.add("is-waiting");
      } else if (step.status === "abandoned") {
        chip.classList.add("is-skipped");
      } else if (step.status === "skipped") {
        if (step.message && step.message.startsWith("Not run —")) {
          chip.classList.add("is-skipped-not-run");
        } else {
          chip.classList.add("is-skipped");
        }
      }
    });
  }

  function showRunAllProgressOptimistic() {
    els.pipelineProgressWrap.hidden = false;
    els.pipelineProgressWrap.classList.add("is-active");
    setProgressBar(els.pipelineProgressBar, 0, null);
    els.pipelineProgressPct.textContent = "0%";
    els.pipelineProgressLabel.textContent = "Starting all steps…";
    els.pipelineProgressEta.textContent = "Preparing pipeline";
    updatePipelineStepStrip([]);
  }

  function beginClientRun(stepIds, { allSteps = false } = {}) {
    clientRunPending = true;
    runAllActive = allSteps;
    activeRunStepIds = stepIds;
    setPipelineBusy(true);
    if (isRunAllActive()) {
      showRunAllProgressOptimistic();
      startProgressTick();
    }
    schedulePoll(POLL_RUNNING_MS);
    if (els.btnAbandonRun) {
      els.btnAbandonRun.hidden = false;
      els.btnAbandonRun.disabled = false;
      els.btnAbandonRun.textContent = "Abandon run";
    }
  }

  function endClientRun() {
    clientRunPending = false;
    runAllActive = false;
  }

  function inferActiveRunSteps(pipeline, steps) {
    if (!pipeline.is_running) {
      if (!clientRunPending) {
        activeRunStepIds = [];
      }
      return;
    }
    if (pipeline.running_step_ids && pipeline.running_step_ids.length) {
      activeRunStepIds = pipeline.running_step_ids;
      return;
    }
    if (activeRunStepIds.length === 0 && pipeline.current_step_id) {
      activeRunStepIds = [pipeline.current_step_id];
    }
  }

  function updateAbandonButton(pipeline) {
    if (!els.btnAbandonRun) {
      return;
    }
    const show =
      pipeline.is_running || (clientRunPending && !pipeline.abandon_requested);
    els.btnAbandonRun.hidden = !show;
    els.btnAbandonRun.disabled =
      !pipeline.is_running || pipeline.abandon_requested === true;
    els.btnAbandonRun.textContent = pipeline.abandon_requested
      ? "Stopping…"
      : "Abandon run";
  }

  function setPipelineBusy(busy) {
    if (!busy && clientRunPending) {
      return;
    }
    els.btnRunAll.disabled = busy;
    els.runStepBtns.forEach((btn) => (btn.disabled = busy));
    setBadge(els.pipelineBadge, busy ? "running" : "idle");
    updateRunAllButton(busy);
    if (lastPipelineSnapshot?.pipeline) {
      updateAbandonButton(lastPipelineSnapshot.pipeline);
    } else if (!busy) {
      if (els.btnAbandonRun) {
        els.btnAbandonRun.hidden = true;
      }
    }
    if (!busy) {
      els.pipelineProgressWrap.hidden = true;
      els.pipelineProgressWrap.classList.remove("is-active");
      if (els.pipelineStepStrip) {
        els.pipelineStepStrip.hidden = true;
      }
      els.pipelineProgressWrap.classList.remove("is-connecting");
      activeRunStepIds = [];
      stopProgressTick();
      lastPipelineSnapshot = null;
    }
  }

  function updateStepCard(card, step, nowMs) {
    if (step.id === 4) {
      return;
    }
    const statusEl = card.querySelector("[data-status]");
    const messageEl = card.querySelector("[data-message]");
    const progressWrap = card.querySelector("[data-progress-wrap]");
    const progressBar = card.querySelector("[data-progress-bar]");
    const progressPct = card.querySelector("[data-progress-pct]");
    const progressEta = card.querySelector("[data-progress-eta]");
    const progressLabel = card.querySelector("[data-progress-label]");
    const steps = lastPipelineSnapshot?.steps || [];
    const leadCount = inferLeadCount(steps);

    setBadge(statusEl, step.status);
    messageEl.textContent = step.message || "—";

    const isRunning =
      step.status === "running" && lastPipelineSnapshot?.pipeline?.is_running;
    card.dataset.active = isRunning ? "true" : "false";
    card.dataset.done =
      step.status === "success" ||
      step.status === "failed" ||
      step.status === "skipped" ||
      step.status === "waiting" ||
      step.status === "abandoned"
        ? step.status
        : "";

    if (isRunning) {
      progressWrap.hidden = false;
      const prog = computeStepProgress(step, nowMs, leadCount, lastTimingConfig);
      setProgressBar(progressBar, prog.pct, prog.variant);
      progressPct.textContent = prog.pct + "%";
      progressEta.textContent = prog.etaText;
      progressLabel.textContent = prog.label;
    } else if (step.status === "waiting") {
      progressWrap.hidden = false;
      const prog = computeStepProgress(step, nowMs, leadCount, lastTimingConfig);
      setProgressBar(progressBar, prog.pct, prog.variant);
      progressPct.textContent = prog.pct + "%";
      progressEta.textContent = prog.etaText;
      progressLabel.textContent = prog.label;
    } else if (step.status === "success" || step.status === "failed") {
      progressWrap.hidden = false;
      const prog = computeStepProgress(step, nowMs, leadCount, lastTimingConfig);
      setProgressBar(progressBar, prog.pct, prog.variant);
      progressPct.textContent = "100%";
      progressEta.textContent = prog.etaText;
      progressLabel.textContent = step.status === "success" ? "Complete" : "Failed";
    } else if (step.status === "skipped") {
      progressWrap.hidden = false;
      const prog = computeStepProgress(step, nowMs, leadCount, lastTimingConfig);
      setProgressBar(progressBar, prog.pct, prog.variant);
      progressPct.textContent = "—";
      progressEta.textContent = prog.etaText;
      progressLabel.textContent = prog.label;
    } else {
      progressWrap.hidden = true;
      setProgressBar(progressBar, 0, null);
    }
  }

  function updatePipelineProgress(pipeline, steps, nowMs) {
    const showProgress =
      pipeline.is_running || (clientRunPending && isRunAllActive());

    if (!showProgress) {
      if (!clientRunPending) {
        els.pipelineProgressWrap.hidden = true;
        els.pipelineProgressWrap.classList.remove("is-active");
      }
      return;
    }

    inferActiveRunSteps(pipeline, steps);
    els.pipelineProgressWrap.hidden = false;
    els.pipelineProgressWrap.classList.add("is-active");

    if (pipeline.is_running) {
      const prog = computePipelineProgress(pipeline, steps, nowMs);
      setProgressBar(els.pipelineProgressBar, prog.pct, null);
      els.pipelineProgressPct.textContent = prog.pct + "%";
      els.pipelineProgressLabel.textContent = isRunAllActive()
        ? "Run all — " + prog.label
        : prog.label;
      els.pipelineProgressEta.textContent = prog.etaText;
    } else if (clientRunPending) {
      setProgressBar(els.pipelineProgressBar, 2, null);
      els.pipelineProgressPct.textContent = "…";
      els.pipelineProgressLabel.textContent = "Starting all steps…";
      els.pipelineProgressEta.textContent = "Connecting to pipeline";
    }

    els.pipelineProgressWrap.classList.toggle(
      "is-connecting",
      clientRunPending && !pipeline.is_running
    );

    updatePipelineStepStrip(steps);
  }

  function updateConfig(config, scheduler, pipeline) {
    els.cfgSchedule.textContent = config.pipeline_run_time + " daily";
    els.cfgScheduler.textContent = config.pipeline_enabled
      ? scheduler.is_running
        ? "Active"
        : "Inactive"
      : "Disabled";
    els.cfgNextRun.textContent = formatTime(scheduler.next_run);
    els.cfgLastRun.textContent = formatTime(pipeline.last_run_at);
    els.cfgSheets.textContent = config.sheets_configured ? "Configured" : "Not configured";
    els.cfgDynamic.textContent = config.sheet_dynamic_lead;
    els.cfgImported.textContent = config.sheet_all_imported;
    els.cfgReady.textContent = config.sheet_ready_to_contact;
  }

  function updateTelegramStatus(telegram) {
    if (!els.cfgTelegram) {
      return;
    }
    if (telegram?.configured) {
      els.cfgTelegram.textContent = "on · " + (telegram.poll_sec || 60) + "s poll";
    } else {
      els.cfgTelegram.textContent = "not configured";
    }
  }

  function updateAttentionPanel(attention, step1Checkpoint) {
    const items = attention?.items || [];
    const checkpointActive = step1Checkpoint?.active;

    if (els.attentionPanel) {
      els.attentionPanel.hidden = items.length === 0 && !checkpointActive;
    }
    if (els.btnResumeStep1) {
      els.btnResumeStep1.hidden = !checkpointActive;
    }
    if (!els.attentionList) {
      return;
    }

    els.attentionList.innerHTML = "";
    items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "attention-item";
      li.innerHTML =
        "<div class=\"attention-item-title\">" +
        escapeHtml(item.title || item.kind || "Attention") +
        "</div>" +
        "<div class=\"attention-item-body\">" +
        escapeHtml(item.body || item.detail || "") +
        "</div>" +
        "<div class=\"attention-item-actions\">" +
        "<button class=\"btn btn-sm btn-ghost btn-dismiss-attention\" type=\"button\" data-id=\"" +
        escapeHtml(item.id) +
        "\">Dismiss</button>" +
        "</div>";
      els.attentionList.appendChild(li);
    });

    els.attentionList.querySelectorAll(".btn-dismiss-attention").forEach((btn) => {
      btn.addEventListener("click", () => dismissAttention(btn.dataset.id));
    });
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function dismissAttention(itemId) {
    if (!itemId) {
      return;
    }
    try {
      await fetchJson("/api/attention/" + encodeURIComponent(itemId) + "/dismiss", {
        method: "POST",
      });
      await refreshStatus();
    } catch (err) {
      showNotification("Could not dismiss item", err.message, "error");
    }
  }

  async function resumeStep1() {
    if (!els.btnResumeStep1) {
      return;
    }
    els.btnResumeStep1.disabled = true;
    els.btnResumeStep1.textContent = "Resuming…";
    beginClientRun([1], { allSteps: false });
    try {
      await fetchJson("/api/pipeline/steps/1/resume", { method: "POST" });
      showNotification("Step 1 resumed", "Continuing from checkpoint", "info");
    } catch (err) {
      showNotification("Resume failed", err.message, "error");
      endClientRun();
    }
    els.btnResumeStep1.disabled = false;
    els.btnResumeStep1.textContent = "Resume Step 1";
    endClientRun();
    await refreshStatus();
  }

  function updateHealth(health) {
    const sheets = health.sheets;
    if (sheets.connected) {
      els.healthStatus.className = "health health-ok";
      els.healthStatus.textContent =
        "Sheets connected — " + (sheets.spreadsheet?.title || "Lead Manager");
    } else if (sheets.configured) {
      els.healthStatus.className = "health health-error";
      els.healthStatus.textContent = "Sheets error: " + (sheets.error || "Connection failed");
    } else {
      els.healthStatus.className = "health health-warn";
      els.healthStatus.textContent = "Sheets not configured — add credentials to .env";
    }
  }

  function setLeadsUploadMessage(text, variant) {
    if (!els.leadsUploadMessage) {
      return;
    }
    if (!text) {
      els.leadsUploadMessage.hidden = true;
      els.leadsUploadMessage.textContent = "";
      els.leadsUploadMessage.className = "leads-message";
      return;
    }
    els.leadsUploadMessage.hidden = false;
    els.leadsUploadMessage.textContent = text;
    els.leadsUploadMessage.className =
      "leads-message" + (variant ? " is-" + variant : "");
  }

  function setModalLeadsMessage(text, variant) {
    if (!els.modalLeadsMessage) {
      return;
    }
    if (!text) {
      els.modalLeadsMessage.hidden = true;
      els.modalLeadsMessage.textContent = "";
      els.modalLeadsMessage.className = "leads-message";
      return;
    }
    els.modalLeadsMessage.hidden = false;
    els.modalLeadsMessage.textContent = text;
    els.modalLeadsMessage.className =
      "leads-message" + (variant ? " is-" + variant : "");
  }

  function closeLeadsUploadModal(result) {
    if (els.leadsUploadModal) {
      els.leadsUploadModal.hidden = true;
      els.leadsUploadModal.setAttribute("aria-hidden", "true");
    }
    setModalLeadsMessage("", null);
    if (leadsUploadModalResolver) {
      leadsUploadModalResolver(result);
      leadsUploadModalResolver = null;
    }
  }

  function readLeadsUploadModalOptions() {
    const content = (els.modalLeadsInput?.value || "").trim();
    if (!content) {
      setModalLeadsMessage(
        "Paste or choose a file, or cancel to abort.",
        "error"
      );
      return null;
    }
    return { step1: { content } };
  }

  function openLeadsUploadModal(mode) {
    return new Promise((resolve) => {
      if (!els.leadsUploadModal || !els.modalLeadsInput) {
        resolve({});
        return;
      }

      leadsUploadModalMode = mode || "step1";
      leadsUploadModalResolver = resolve;

      els.modalLeadsInput.value = "";
      if (els.modalLeadsFileInput) {
        els.modalLeadsFileInput.value = "";
      }

      if (els.leadsUploadConfirm) {
        els.leadsUploadConfirm.textContent =
          leadsUploadModalMode === "run_all" ? "Import and continue" : "Import and run";
      }

      setModalLeadsMessage("", null);
      els.leadsUploadModal.hidden = false;
      els.leadsUploadModal.setAttribute("aria-hidden", "false");
      els.modalLeadsInput.focus();
    });
  }

  function handleModalLeadsFileSelect(event) {
    const file = event.target.files?.[0];
    if (!file || !els.modalLeadsInput) {
      return;
    }
    file
      .text()
      .then((text) => {
        els.modalLeadsInput.value = text;
        setModalLeadsMessage("Loaded " + file.name + " — click Import to run.", null);
      })
      .catch((err) => {
        setModalLeadsMessage("Could not read file: " + err.message, "error");
      });
    event.target.value = "";
  }

  async function maybeStep1Options(stepId, includeStep1) {
    if (stepId === 1 || includeStep1) {
      const mode = includeStep1 ? "run_all" : "step1";
      const modalResult = await openLeadsUploadModal(mode);
      if (!modalResult) {
        return null;
      }
      return modalResult;
    }
    return {};
  }

  async function uploadLeads() {
    if (!els.leadsUploadInput || !els.btnUploadLeads) {
      return;
    }
    const content = els.leadsUploadInput.value.trim();
    if (!content) {
      setLeadsUploadMessage("Paste or choose a lead list first.", "error");
      return;
    }

    els.btnUploadLeads.disabled = true;
    els.btnUploadLeads.textContent = "Importing…";
    setLeadsUploadMessage("", null);

    try {
      const result = await fetchJson("/api/leads/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      const appended = result.rows_appended || 0;
      const msg = result.message || "Import complete";
      setLeadsUploadMessage(msg, appended > 0 ? "success" : null);
      showNotification(
        appended > 0 ? appended + " lead(s) imported" : "No new leads",
        msg,
        appended > 0 ? "success" : "warning"
      );
      if (appended > 0) {
        els.leadsUploadInput.value = "";
      }
      await refreshStatus();
    } catch (err) {
      setLeadsUploadMessage(err.message, "error");
      showNotification("Lead import failed", err.message, "error");
    } finally {
      els.btnUploadLeads.disabled = false;
      els.btnUploadLeads.textContent = "Import to sheet";
    }
  }

  function handleLeadsFileSelect(event) {
    const file = event.target.files?.[0];
    if (!file || !els.leadsUploadInput) {
      return;
    }
    file
      .text()
      .then((text) => {
        els.leadsUploadInput.value = text;
        setLeadsUploadMessage("Loaded " + file.name + " — click Import to sheet.", null);
      })
      .catch((err) => {
        setLeadsUploadMessage("Could not read file: " + err.message, "error");
      });
    event.target.value = "";
  }

  function renderLogs(logs) {
    if (!logs.length && !localLogView.length) {
      els.logPanel.textContent = "Waiting for pipeline activity…";
      els.logPanel.classList.remove("has-logs");
      return;
    }
    const combined = [...logs];
    els.logPanel.textContent = combined.join("\n");
    els.logPanel.classList.add("has-logs");
    els.logPanel.scrollTop = els.logPanel.scrollHeight;
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    return res.json();
  }

  function schedulePoll(ms) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refreshStatus, ms);
  }

  function startProgressTick() {
    if (progressTick) return;
    progressTick = setInterval(() => {
      const pipelineRunning = lastPipelineSnapshot?.pipeline?.is_running;
      const needsLive =
        pipelineRunning ||
        clientRunPending ||
        step4NeedsLiveUpdates(lastPipelineSnapshot?.steps || []);
      if (!needsLive) {
        stopProgressTick();
        return;
      }
      const nowMs = Date.now();
      const snapshot = lastPipelineSnapshot || {
        pipeline: { is_running: false, current_step_id: null },
        steps: [],
      };
      const { pipeline, steps } = snapshot;
      updatePipelineProgress(pipeline, steps, nowMs);
      steps.forEach((step) => {
        const card = document.querySelector(".step-card[data-step-id=\"" + step.id + "\"]");
        if (card) updateStepCard(card, step, nowMs);
      });
    }, 250);
  }

  function stopProgressTick() {
    if (progressTick) {
      clearInterval(progressTick);
      progressTick = null;
    }
  }

  async function refreshStatus() {
    try {
      const [status, health, logsData] = await Promise.all([
        fetchJson("/api/status"),
        fetchJson("/api/health"),
        fetchJson("/api/pipeline/logs?limit=100"),
      ]);
      const { pipeline, scheduler, config, timing } = status;
      const nowMs = Date.now();
      lastTimingConfig = timing || null;
      lastPipelineSnapshot = { pipeline, steps: pipeline.steps };

      const pipelineBusy = pipeline.is_running || clientRunPending;
      setPipelineBusy(pipelineBusy);

      const needsFastPoll = step4NeedsLiveUpdates(pipeline.steps);
      if (needsFastPoll) {
        schedulePoll(POLL_RUNNING_MS);
        startProgressTick();
      } else if (pipelineBusy) {
        schedulePoll(POLL_PIPELINE_MS);
        startProgressTick();
      } else {
        schedulePoll(POLL_IDLE_MS);
        if (!clientRunPending) {
          stopProgressTick();
        }
      }

      updateConfig(config, scheduler, pipeline);
      updateTelegramStatus(status.telegram);
      updateAttentionPanel(status.attention, status.step1_checkpoint);
      updateHealth(health);
      updatePipelineProgress(pipeline, pipeline.steps, nowMs);
      updatePipelineOutcome(pipeline);
      updateAbandonButton(pipeline);

      if (!notificationsSeeded) {
        seedNotificationKeys(pipeline, pipeline.steps);
        notificationsSeeded = true;
      } else {
        processNotifications(pipeline, pipeline.steps);
      }

      pipeline.steps.forEach((step) => {
        if (step.id === 4) {
          return;
        }
        const card = document.querySelector(".step-card[data-step-id=\"" + step.id + "\"]");
        if (card) updateStepCard(card, step, nowMs);
      });

      renderLogs(logsData.logs);
    } catch (err) {
      console.error("Status refresh failed:", err);
    }
  }

  async function runAll() {
    const options = await maybeStep1Options(null, true);
    if (!options) {
      return;
    }
    beginClientRun([1, 2, 3, 4, 5, 6, 7, 8], { allSteps: true });
    localLogView.push("[" + new Date().toLocaleTimeString() + "] Manual run all triggered");
    renderLogs(localLogView);
    refreshStatus();
    try {
      await fetchJson("/api/pipeline/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options }),
      });
    } catch (err) {
      alert("Run failed: " + err.message);
      showNotification("Pipeline run failed", err.message, "error");
      endClientRun();
      setPipelineBusy(false);
      schedulePoll(POLL_IDLE_MS);
      return;
    }
    endClientRun();
    await refreshStatus();
  }

  async function runStep(stepId) {
    const id = Number(stepId);
    if (id === 4) {
      return;
    }
    const options = await maybeStep1Options(id, false);
    if (!options) {
      return;
    }
    beginClientRun([Number(stepId)], { allSteps: false });
    localLogView.push(
      "[" + new Date().toLocaleTimeString() + "] Manual run step " + stepId + " triggered"
    );
    renderLogs(localLogView);
    try {
      await fetchJson("/api/pipeline/steps/" + stepId + "/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options }),
      });
    } catch (err) {
      alert("Step run failed: " + err.message);
      showNotification("Step " + stepId + " failed", err.message, "error");
      endClientRun();
      setPipelineBusy(false);
      schedulePoll(POLL_IDLE_MS);
      return;
    }
    endClientRun();
    await refreshStatus();
  }

  async function abandonRun() {
    if (
      !lastPipelineSnapshot?.pipeline?.is_running &&
      !clientRunPending
    ) {
      return;
    }
    if (
      !confirm(
        "Abandon the current pipeline run? The active step will stop at the next checkpoint."
      )
    ) {
      return;
    }
    try {
      await fetchJson("/api/pipeline/abandon", { method: "POST" });
      showNotification(
        "Abandon requested",
        "Pipeline will stop at the next checkpoint",
        "warning"
      );
      if (els.btnAbandonRun) {
        els.btnAbandonRun.disabled = true;
        els.btnAbandonRun.textContent = "Stopping…";
      }
      await refreshStatus();
    } catch (err) {
      alert("Could not abandon run: " + err.message);
    }
  }

  els.btnRunAll.addEventListener("click", runAll);
  if (els.btnAbandonRun) {
    els.btnAbandonRun.addEventListener("click", abandonRun);
  }
  els.runStepBtns.forEach((btn) => {
    btn.addEventListener("click", () => runStep(btn.dataset.stepId));
  });
  els.btnClearLogs.addEventListener("click", () => {
    localLogView = [];
    els.logPanel.textContent = "Log view cleared. Server logs will repopulate on next poll.";
    els.logPanel.classList.remove("has-logs");
  });
  if (els.btnUploadLeads) {
    els.btnUploadLeads.addEventListener("click", uploadLeads);
  }
  if (els.leadsFileInput) {
    els.leadsFileInput.addEventListener("change", handleLeadsFileSelect);
  }
  if (els.modalLeadsFileInput) {
    els.modalLeadsFileInput.addEventListener("change", handleModalLeadsFileSelect);
  }
  if (els.leadsUploadCancel) {
    els.leadsUploadCancel.addEventListener("click", () => closeLeadsUploadModal(null));
  }
  if (els.leadsUploadConfirm) {
    els.leadsUploadConfirm.addEventListener("click", () => {
      const options = readLeadsUploadModalOptions();
      if (!options) {
        return;
      }
      if (els.leadsUploadInput && els.modalLeadsInput) {
        els.leadsUploadInput.value = els.modalLeadsInput.value;
      }
      closeLeadsUploadModal(options);
    });
  }
  if (els.leadsUploadModal) {
    els.leadsUploadModal.addEventListener("click", (event) => {
      if (event.target === els.leadsUploadModal) {
        closeLeadsUploadModal(null);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      els.leadsUploadModal &&
      !els.leadsUploadModal.hidden
    ) {
      closeLeadsUploadModal(null);
    }
  });
  if (els.btnResumeStep1) {
    els.btnResumeStep1.addEventListener("click", resumeStep1);
  }

  function resetInitialUiState() {
    if (els.leadsUploadModal) {
      els.leadsUploadModal.hidden = true;
      els.leadsUploadModal.setAttribute("aria-hidden", "true");
    }
    els.pipelineProgressWrap.hidden = true;
    if (els.pipelineOutcome) {
      els.pipelineOutcome.hidden = true;
    }
    if (els.pipelineStepStrip) {
      els.pipelineStepStrip.hidden = true;
    }
    if (els.btnRunAllSpinner) {
      els.btnRunAllSpinner.hidden = true;
    }
    els.btnRunAll.classList.remove("btn-run-all-busy");
    setPipelineBusy(false);
  }

  resetInitialUiState();
  refreshStatus();
  schedulePoll(POLL_IDLE_MS);
})();
