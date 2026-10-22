/* Model families. One place maps a run or a model id to its family (hue) and model (shade of that hue).
   dash/models.py holds the same rules for the server-side counts. Any id
   containing beam reads as Beam (beam-<ver>, else beam) whichever harness
   ran it, before the Command Code free-id check. A Grok CLI
   run with a recorded-but-unknown model id (mimo-*, step-*) reads as Other;
   only an empty id reads as glm-unknown. Codex ids (gpt-*, codex-*,
   gpt-reserve) read as Codex under their id; an empty id with a codex
   tier or source reads as codex-unknown. harnessOf reads the runner
   (pi, commandcode, grok-cli for a glm source) from the source, and the
   tier when the source is generic; a run view carrying harness wins. */
(function (global) {
  "use strict";
  var FAMS = ["claude", "cursor", "muse", "glm", "deepseek", "beam", "codex", "commandcode", "dagster", "other"];
  var FAM = {
    claude: {name: "Claude", hue: "--s1", order: ["opus", "fable", "sonnet", "haiku"]},
    cursor: {name: "Cursor", hue: "--s4", order: ["grok-4.7", "composer-2.5"]},
    muse: {name: "muse", hue: "--s5", order: ["muse-spark-1.3", "muse-wonder-1.0"]},
    glm: {name: "GLM", hue: "--s6", order: ["glm-5.3", "glm-5.3-flash"]},
    deepseek: {name: "DeepSeek", hue: "--s8", order: ["deepseek-flash", "deepseek-v4-pro"]},
    beam: {name: "Beam", hue: "--s11", order: ["beam-1.0", "beam"]},
    codex: {name: "Codex", hue: "--s9", order: ["gpt-6.1-sol", "gpt-6-luna"]},
    commandcode: {name: "Command Code", hue: "--s10", order: ["minimax/minimax-m3.1-flash:free", "ornith/ornith-1.5-122b:free", "poolside/laguna-s-2.1-free", "stealth/glyph-cluster:free"]},
    dagster: {name: "Dagster", hue: "--s7", order: []},
    other: {name: "Other", hue: "--other", order: ["other"]}
  };
  var WORD = {grok: "Grok", composer: "Composer", glm: "GLM", flash: "Flash", beam: "Beam", spark: "Spark", wonder: "Wonder", muse: "muse", opus: "Opus", fable: "Fable", sonnet: "Sonnet", haiku: "Haiku", other: "Other", deepseek: "DeepSeek", pro: "Pro", codex: "Codex", gpt: "GPT", sol: "Sol", luna: "Luna", astra: "Astra", terra: "Terra", reserve: "Reserve", auto: "Auto", review: "Review"};
  /* Harness display names. harnessOf reads the runner from a run row
     (dash/models.py harness_of mirrors it); a run view carrying harness
     wins, so server and page never disagree. */
  var HARNESS = {claude: "Claude Code", muse: "muse", cursor: "Cursor", "grok-cli": "Grok Build", pi: "pi", commandcode: "Command Code", codex: "Codex", dagster: "dagster", generic: "generic"};
  function harnessOf(r) {
    r = r || {};
    if (r.harness) return r.harness;
    var source = String(r.source || ""), tier = String(r.tier || "");
    var key = (source === "generic" ? tier : source).toLowerCase();
    if (/^claude/.test(key) || /^(opus|sonnet|haiku|fable)$/.test(key)) return "claude";
    if (key === "muse") return "muse";
    if (key === "grok" || key === "cursor" || key === "composer") return "cursor";
    if (key === "glm") return "grok-cli";
    if (key === "pi") return "pi";
    if (key === "commandcode") return "commandcode";
    if (/^codex/.test(key)) return "codex";
    if (key === "dagster") return "dagster";
    if (key === "generic") return "generic";
    return source || "generic";
  }
  function harnessName(h) { return HARNESS[h] || h || "generic"; }

  var COMMANDCODE_FREE = ["reflection/beam:free", "minimax/minimax-m3.1-flash:free", "ornith/ornith-1.5-122b:free", "poolside/laguna-s-2.1-free", "stealth/glyph-cluster:free"];

  function keyOf(model, hint) {
    var m = String(model || "").toLowerCase(), h = String(hint || "").toLowerCase(), v;
    if (/^dagster:/.test(m) || /dagster/.test(h)) {
      var job = /^dagster:/.test(m) ? m.slice(8) : m;
      return ["dagster", "dagster:" + (job || "job")];
    }
    if (/beam/.test(m) || (/beam/.test(h) && !m)) { v = /beam[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["beam", m ? "beam" + (v ? "-" + v[1] : "") : "beam-unknown"]; }
    if (/commandcode/.test(h) || COMMANDCODE_FREE.indexOf(m) >= 0) return ["commandcode", m || "commandcode-unknown"];
    v = /^(beam|claude|cursor|muse|glm|codex)-unknown$/.exec(m);
    if (v) return [v[1], m];
    if (/composer/.test(m)) { v = /composer[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["cursor", "composer" + (v ? "-" + v[1] : "")]; }
    if (/grok/.test(m)) { v = /grok[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["cursor", "grok" + (v ? "-" + v[1] : "")]; }
    if (/glm/.test(m)) { v = /glm[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["glm", "glm" + (v ? "-" + v[1] : "") + (/flash/.test(m) ? "-flash" : "")]; }
    if (/deepseek/.test(m)) { return ["deepseek", /pro/.test(m) ? "deepseek-v4-pro" : "deepseek-flash"]; }
    if (/muse/.test(m)) { v = /muse-(spark|wonder)(?:-full)?-(\d+(?:\.\d+)?)/.exec(m); return ["muse", v ? "muse-" + v[1] + "-" + v[2] : "muse-unknown"]; }
    if (/codex/.test(h) || m === "codex-unknown") return ["codex", m || "codex-unknown"];
    if (/^gpt-/.test(m) || m === "gpt-reserve" || /^codex-/.test(m)) return ["codex", m];
    v = /(opus|fable|sonnet|haiku)/.exec(m) || /(opus|fable|sonnet|haiku)/.exec(h);
    if (v) return ["claude", v[1]];
    if (/grok|cursor|composer/.test(h)) return ["cursor", "cursor-unknown"];
    if (/muse/.test(h)) return ["muse", "muse-unknown"];
    /* A recorded-but-unknown Grok CLI id (mimo-*, step-*) is Other; only an
       empty id falls back to glm-unknown. Mirrors dash/models.py. */
    if (/glm/.test(h)) return m ? ["other", "other"] : ["glm", "glm-unknown"];
    if (/claude/.test(h)) return ["claude", "claude-unknown"];
    return ["other", "other"];
  }
  function nameOf(key) {
    if (/^dagster:/.test(key)) return key.slice(8) || "job";
    if (famOf(key) === "commandcode") return key;
    if (/-unknown$/.test(key)) return "model not recorded";
    if (key === "deepseek-flash") return "DeepSeek V4.1 Flash";
    if (key === "deepseek-v4-pro") return "DeepSeek V4 Pro";
    return key.split("-").map(function (w) { return WORD[w] || w; }).join(" ");
  }
  function famOf(key) { return keyOf(key)[0]; }
  function shadeIndex(fam, key) {
    var order = FAM[fam].order, i = order.indexOf(key);
    if (/-unknown$/.test(key)) return 999;
    if (i < 0) { order.push(key); i = order.length - 1; }
    return i;
  }
  /* One rung of a family hue: the shade logic colorOf runs per model,
     reused so a second harness of the same model takes another shade of
     the same hue instead of a new colour. */
  function colorAt(fam, i) {
    var hue = "var(" + (FAM[fam] || FAM.other).hue + ")";
    if (i === 999) return "color-mix(in oklab, " + hue + " 35%, var(--muted))";
    return [hue, "color-mix(in oklab, " + hue + " 55%, var(--surface))", "color-mix(in oklab, " + hue + " 65%, var(--ink))", "color-mix(in oklab, " + hue + " 30%, var(--surface))", "color-mix(in oklab, " + hue + " 45%, var(--ink))"][i % 5];
  }
  function colorOf(key) {
    var fam = famOf(key);
    return colorAt(fam, shadeIndex(fam, key));
  }
  /* A series key is a model key plus its harness (beam-1.0@pi); the model
     half keeps every model derivation, the harness half only names the
     runner. Single-harness series keep the model colour exactly; sibling
     harnesses of one model spread across the hue's rungs. */
  function splitSeries(key) {
    var s = String(key == null ? "" : key), at = s.lastIndexOf("@"), h = at < 0 ? "" : s.slice(at + 1);
    if (at < 0 || !h) return {model: s, harness: ""};
    return {model: s.slice(0, at), harness: h};
  }
  function seriesName(key) {
    var p = splitSeries(key), n = nameOf(p.model);
    return p.harness ? n + " \u00B7 " + harnessName(p.harness) : n;
  }
  function seriesFam(key) { return famOf(splitSeries(key).model); }
  function seriesColor(key, siblings) {
    var p = splitSeries(key), fam = seriesFam(key), i = shadeIndex(fam, p.model);
    var sibs = (siblings || []).map(function (k) { return splitSeries(k).harness; }).filter(function (h, ix, all) { return h && all.indexOf(h) === ix; }).sort();
    if (sibs.length < 2 || sibs.indexOf(p.harness) < 0) return colorAt(fam, i);
    return colorAt(fam, i + sibs.indexOf(p.harness));
  }
  /* Series order without growing the family order lists: family rank,
     then model, then harness. shadeIndex would append every series key
     to FAM order as it sorts; this never touches it. */
  function sortSeries(keys) {
    return keys.slice().sort(function (a, b) {
      var pa = splitSeries(a), pb = splitSeries(b);
      var fa = seriesFam(a), fb = seriesFam(b);
      return FAMS.indexOf(fa) - FAMS.indexOf(fb) || (pa.model < pb.model ? -1 : pa.model > pb.model ? 1 : 0) || (pa.harness < pb.harness ? -1 : pa.harness > pb.harness ? 1 : 0);
    });
  }
  function info(x) {
    var r = x && typeof x === "object" ? x : {model: x};
    var k = keyOf(r.model, [r.tier, r.source].join(" "));
    return {key: k[1], fam: k[0], name: nameOf(k[1]), famName: FAM[k[0]].name, full: FAM[k[0]].name + " · " + nameOf(k[1]), color: colorOf(k[1])};
  }
  function sortKeys(keys) {
    return keys.slice().sort(function (a, b) {
      var fa = famOf(a), fb = famOf(b);
      return FAMS.indexOf(fa) - FAMS.indexOf(fb) || shadeIndex(fa, a) - shadeIndex(fb, b) || a.localeCompare(b);
    });
  }
  function famColor(fam) { return "var(" + (FAM[fam] || FAM.other).hue + ")"; }
  /* Run outcome classes, the one client derivation. Mirrors
     dash/outcomes.py outcome_class/outcome_reason exactly: the server
     exposes run.outcome_class (plus run.outcome_reason) on its views,
     and outcomeOf trusts it when present, so the timeline, the replay,
     the tile caption, the tables, and the drawer never keep their own
     if-chains. Every class ships colour plus glyph plus word, never
     colour alone; blocked (orange, a reported verdict) and died (red,
     the session ended without one) never share a look. */
  var OUTCOME = {
    run: ["var(--s1)", "\u25CF", "running"],
    ok: ["var(--good)", "\u2713", "finished"],
    part: ["var(--warning)", "\u25D0", "partial"],
    blocked: ["var(--serious)", "\u2298", "blocked"],
    died: ["var(--critical)", "\u2715", "died"],
    cancelled: ["var(--muted)", "\u23F9", "cancelled"],
    stalled: ["var(--warning)", "\u25D4", "stalled"],
    unk: ["var(--muted)", "\u25CB", "no verdict"]
  };
  function outcomeOf(r) {
    r = r || {};
    if (r.outcome_class && OUTCOME[r.outcome_class]) return r.outcome_class;
    var st = r.status, v = r.verdict == null ? null : String(r.verdict).toUpperCase();
    if (r.stalled && (st === "running" || st === "queued") && r.ended_at == null) return "stalled";
    if (r.ended_at == null) return st === "running" ? "run" : "unk";
    if (st === "killed" || st === "died_early") return "died";
    if (st === "unknown") return "unk";
    if (v === "FAILURE" || v === "FAIL" || v === "UNRESOLVED" || v === "QUOTA" || v === "IDLE") return "died";
    if (v === "CANCELED") return "cancelled";
    if (v === "BLOCKED" || v === "REQUEST_CHANGES" || v === "FIX") return "blocked";
    if (v === "PARTIAL" || v === "APPROVE_WITH_CHANGES") return "part";
    if (v && (v.indexOf("APPROVE") === 0 || v.indexOf("PASS") === 0 || v.indexOf("SUCCESS") === 0 || v.indexOf("SHIP") === 0)) return "ok";
    if (st === "done" || st === "finished") return "ok";
    return "unk";
  }
  function outcomeReason(r, cls) {
    r = r || {};
    if (r.outcome_reason != null) return r.outcome_reason;
    var o = cls || outcomeOf(r), v = r.verdict == null ? null : String(r.verdict).toUpperCase();
    if (o === "died") {
      if (r.status === "killed") return "terminated: SIGTERM trace in the err log";
      if (v === "QUOTA") return "quota/rate limit ended the run";
      if (v === "IDLE") return "closed idle: no output, session never finished";
      if (v === "FAILURE" || v === "FAIL") return "reported FAILURE";
      if (v === "UNRESOLVED") return "ended unresolved";
      return "process exited without a verdict";
    }
    if (o === "stalled") {
      var age = (typeof r.stalled === "number") ? r.stalled : null;
      var when = age == null ? null : (age < 90 ? Math.floor(age) + "s" : (age < 5400 ? Math.floor(age / 60) + "m" : (Math.round(age / 360) / 10) + "h"));
      return when ? "no output for " + when + "; process still alive" : "no output; process still alive";
    }
    if (o === "unk") return "no verdict recorded";
    return null;
  }
  global.ADM = {FAMS: FAMS, FAM: FAM, HARNESS: HARNESS, info: info, keyOf: keyOf, nameOf: nameOf, famOf: famOf, colorOf: colorOf, colorAt: colorAt, famColor: famColor, sortKeys: sortKeys, harnessOf: harnessOf, harnessName: harnessName, splitSeries: splitSeries, seriesName: seriesName, seriesFam: seriesFam, seriesColor: seriesColor, sortSeries: sortSeries, OUTCOME: OUTCOME, outcomeOf: outcomeOf, outcomeReason: outcomeReason};
})(window);
