/* Model families. One place maps a run or a model id to its family (hue) and model (shade of that hue).
   dash/models.py holds the same rules for the server-side counts. */
(function (global) {
  "use strict";
  var FAMS = ["claude", "cursor", "muse", "glm", "other"];
  var FAM = {
    claude: {name: "Claude", hue: "--s1", order: ["opus", "fable", "sonnet", "haiku"]},
    cursor: {name: "Cursor", hue: "--s4", order: ["grok-4.7", "composer-2.5"]},
    muse: {name: "muse", hue: "--s5", order: ["muse-spark-1.3", "muse-wonder-1.0"]},
    glm: {name: "GLM", hue: "--s6", order: ["glm-5.3", "glm-5.3-flash"]},
    other: {name: "Other", hue: "--other", order: ["other"]}
  };
  var WORD = {grok: "Grok", composer: "Composer", glm: "GLM", flash: "Flash", spark: "Spark", wonder: "Wonder", muse: "muse", opus: "Opus", fable: "Fable", sonnet: "Sonnet", haiku: "Haiku", other: "Other"};

  function keyOf(model, hint) {
    var m = String(model || "").toLowerCase(), h = String(hint || "").toLowerCase(), v;
    v = /^(claude|cursor|muse|glm)-unknown$/.exec(m);
    if (v) return [v[1], m];
    if (/composer/.test(m)) { v = /composer[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["cursor", "composer" + (v ? "-" + v[1] : "")]; }
    if (/grok/.test(m)) { v = /grok[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["cursor", "grok" + (v ? "-" + v[1] : "")]; }
    if (/glm/.test(m)) { v = /glm[-_ ]?(\d+(?:\.\d+)?)/.exec(m); return ["glm", "glm" + (v ? "-" + v[1] : "") + (/flash/.test(m) ? "-flash" : "")]; }
    if (/muse/.test(m)) { v = /muse-(spark|wonder)(?:-full)?-(\d+(?:\.\d+)?)/.exec(m); return ["muse", v ? "muse-" + v[1] + "-" + v[2] : "muse-unknown"]; }
    v = /(opus|fable|sonnet|haiku)/.exec(m) || /(opus|fable|sonnet|haiku)/.exec(h);
    if (v) return ["claude", v[1]];
    if (/grok|cursor|composer/.test(h)) return ["cursor", "cursor-unknown"];
    if (/muse/.test(h)) return ["muse", "muse-unknown"];
    if (/glm/.test(h)) return ["glm", "glm-unknown"];
    if (/claude/.test(h)) return ["claude", "claude-unknown"];
    return ["other", "other"];
  }
  function nameOf(key) {
    if (/-unknown$/.test(key)) return "model not recorded";
    return key.split("-").map(function (w) { return WORD[w] || w; }).join(" ");
  }
  function famOf(key) { return keyOf(key)[0]; }
  function shadeIndex(fam, key) {
    var order = FAM[fam].order, i = order.indexOf(key);
    if (/-unknown$/.test(key)) return 999;
    if (i < 0) { order.push(key); i = order.length - 1; }
    return i;
  }
  function colorOf(key) {
    var fam = famOf(key), hue = "var(" + FAM[fam].hue + ")", i = shadeIndex(fam, key);
    if (i === 999) return "color-mix(in oklab, " + hue + " 35%, var(--muted))";
    return [hue, "color-mix(in oklab, " + hue + " 55%, var(--surface))", "color-mix(in oklab, " + hue + " 65%, var(--ink))", "color-mix(in oklab, " + hue + " 30%, var(--surface))", "color-mix(in oklab, " + hue + " 45%, var(--ink))"][i % 5];
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
  global.ADM = {FAMS: FAMS, FAM: FAM, info: info, keyOf: keyOf, nameOf: nameOf, famOf: famOf, colorOf: colorOf, famColor: famColor, sortKeys: sortKeys};
})(window);
