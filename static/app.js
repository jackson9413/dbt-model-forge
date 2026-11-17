/* dbt-model-forge frontend. Plain DOM building, no template literals,
   so pasted model text is always shown as text and never as markup. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          throw new Error(data && data.error ? data.error : "request failed");
        }
        return data;
      });
    });
  }

  var state = { result: null, severity: "all", axis: "all" };

  function status(message, isError) {
    var node = document.getElementById("status");
    node.textContent = message || "";
    node.className = isError ? "status err" : "status";
  }

  /* ---------------------------------------------------------------- boot */

  function loadProfiles() {
    return api("/api/profiles").then(function (list) {
      var select = document.getElementById("profile");
      clear(select);
      list.forEach(function (item) {
        var option = el("option", null, item.label);
        option.value = item.key;
        select.appendChild(option);
      });
    });
  }

  function loadSampleList() {
    return api("/api/samples").then(function (list) {
      var select = document.getElementById("sample-select");
      clear(select);
      var blank = el("option", null, "Choose a sample model...");
      blank.value = "";
      select.appendChild(blank);
      list.forEach(function (item) {
        var option = el("option", null, item.title);
        option.value = item.id;
        select.appendChild(option);
      });
    });
  }

  function loadSample(id) {
    if (!id) return;
    api("/api/samples/" + id).then(function (sample) {
      document.getElementById("sql").value = sample.sql || "";
      document.getElementById("yaml").value = sample.yaml || "";
      document.getElementById("model-name").value = sample.model_name || "";
      status("Loaded " + (sample.title || id));
      run();
    }).catch(function (err) { status(err.message, true); });
  }

  /* ------------------------------------------------------------- analysis */

  function run() {
    var sql = document.getElementById("sql").value;
    if (!sql.trim()) { status("Paste a dbt model first.", true); return; }
    status("Reviewing...");
    var payload = {
      sql: sql,
      yaml: document.getElementById("yaml").value,
      model_name: document.getElementById("model-name").value,
      profile: document.getElementById("profile").value,
      save: document.getElementById("save").checked
    };
    api("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (result) {
      state.result = result;
      render(result);
      status("Done - " + result.findings.length + " finding(s).");
      if (result.run_id) loadRuns();
    }).catch(function (err) { status(err.message, true); });
  }

  /* ------------------------------------------------------------ rendering */

  function render(result) {
    document.getElementById("empty").hidden = true;
    var out = document.getElementById("output");
    clear(out);
    out.hidden = false;
    out.appendChild(renderScore(result));
    out.appendChild(renderCards(result));
    out.appendChild(renderFindings(result));
    out.appendChild(renderBlueprint(result));
    out.appendChild(renderPlan(result));
  }

  function renderScore(result) {
    var score = result.score;
    var wrap = el("div", "score-head");
    var num = el("div", "score-num", score.overall);
    num.appendChild(el("small", null, " / 100"));
    wrap.appendChild(num);

    var meta = el("div", "score-meta");
    var verdict = el("span", "verdict v-" + score.verdict, score.verdict.replace(/_/g, " "));
    meta.appendChild(verdict);
    meta.appendChild(el("div", null, result.model + "  -  " + score.profile_label));
    var summary = el("div", "muted", score.verdict_text);
    meta.appendChild(summary);
    wrap.appendChild(meta);

    var counts = el("div", "score-meta");
    var critical = 0, high = 0;
    result.findings.forEach(function (f) {
      if (f.severity === "critical") critical += 1;
      if (f.severity === "high") high += 1;
    });
    counts.appendChild(el("div", null, critical + " critical, " + high + " high"));
    counts.appendChild(el("div", "muted", result.findings.length + " findings in total"));
    wrap.appendChild(counts);

    var section = el("div", "section");
    var axes = el("div", "axes");
    score.axes.forEach(function (axis) {
      var row = el("div", "axis" + (axis.capped ? " capped" : ""));
      row.appendChild(el("div", null, axis.label));
      var bar = el("div", "bar");
      var fill = el("div", "fill");
      if (!axis.assessed) {
        fill.style.width = "0%";
      } else {
        var value = axis.score;
        fill.style.width = value + "%";
        if (value < 45) fill.classList.add("bad");
        else if (value < 75) fill.classList.add("warn");
      }
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("div", "num", axis.assessed ? axis.score + "  (" + axis.weight + "%)" : "not assessed"));
      axes.appendChild(row);
    });
    section.appendChild(axes);
    (result.notes || []).forEach(function (note) {
      section.appendChild(el("div", "note", note));
    });
    var holder = el("div");
    holder.appendChild(wrap);
    holder.appendChild(section);
    return holder;
  }

  function renderCards(result) {
    var cards = el("div", "cards");

    var grain = el("div", "card");
    grain.appendChild(el("h3", null, "Row grain"));
    grain.appendChild(el("div", "big", result.grain.description));
    grain.appendChild(el("div", "muted", "declared via " + result.grain.method +
      " (confidence: " + result.grain.confidence + ")"));
    cards.appendChild(grain);

    var mat = el("div", "card");
    mat.appendChild(el("h3", null, "Materialisation"));
    mat.appendChild(el("div", "big", result.materialization));
    if (result.materialization === "incremental") {
      mat.appendChild(el("div", "muted", "guard: " + (result.incremental.guard ? "present" : "MISSING")));
      mat.appendChild(el("div", "muted", "unique_key: " + (result.incremental.unique_key || "MISSING")));
      mat.appendChild(el("div", "muted", "filter: " + (result.incremental.filter || "MISSING")));
    }
    var tags = result.tags || [];
    mat.appendChild(el("div", "muted", "tags: " + (tags.length ? tags.join(", ") : "none")));
    cards.appendChild(mat);

    var deps = el("div", "card");
    deps.appendChild(el("h3", null, "Dependencies"));
    var list = el("ul");
    result.dependencies.relations.forEach(function (rel) {
      list.appendChild(el("li", null, rel.role + "  " + (rel.name || "?") +
        "  [" + rel.kind + "]" + (rel.alias ? " as " + rel.alias : "")));
    });
    result.dependencies.refs.forEach(function (ref) {
      list.appendChild(el("li", null, "ref: " + ref.expr));
    });
    result.dependencies.sources.forEach(function (src) {
      list.appendChild(el("li", null, "source: " + src.expr));
    });
    if (!list.firstChild) list.appendChild(el("li", null, "none declared"));
    deps.appendChild(list);
    cards.appendChild(deps);

    var cols = el("div", "card");
    cols.appendChild(el("h3", null, "Output columns (" + result.columns.length + ")"));
    var clist = el("ul");
    result.columns.slice(0, 14).forEach(function (col) {
      var text = col.name + (col.aggregates.length ? "  (" + col.aggregates.join(", ") + ")" : "");
      clist.appendChild(el("li", null, text));
    });
    cols.appendChild(clist);
    cards.appendChild(cols);

    return cards;
  }

  function renderFindings(result) {
    var section = el("div", "section");
    section.appendChild(el("h2", null, "Findings (" + result.findings.length + ")"));

    var severities = ["all", "critical", "high", "medium", "low"];
    var chips = el("div", "chips");
    severities.forEach(function (sev) {
      var chip = el("button", "chip" + (state.severity === sev ? " on" : ""), sev);
      chip.addEventListener("click", function () {
        state.severity = sev;
        render(result);
      });
      chips.appendChild(chip);
    });
    section.appendChild(chips);

    var axes = ["all", "grain", "perf", "lineage", "testing", "conventions"];
    var axisChips = el("div", "chips");
    axes.forEach(function (axis) {
      var chip = el("button", "chip" + (state.axis === axis ? " on" : ""), axis);
      chip.addEventListener("click", function () {
        state.axis = axis;
        render(result);
      });
      axisChips.appendChild(chip);
    });
    section.appendChild(axisChips);

    var shown = result.findings.filter(function (f) {
      if (state.severity !== "all" && f.severity !== state.severity) return false;
      if (state.axis !== "all" && f.axis !== state.axis) return false;
      return true;
    });

    if (!shown.length) {
      section.appendChild(el("div", "muted", "No findings match this filter."));
    }

    shown.forEach(function (f) {
      var card = el("div", "finding " + f.severity);
      var head = el("h4");
      head.appendChild(el("span", "fid", f.id));
      head.appendChild(el("span", "sev", f.severity));
      head.appendChild(el("span", null, f.title));
      head.appendChild(el("span", "axis-tag", f.axis_label));
      card.appendChild(head);
      card.appendChild(el("div", "ev", f.evidence));
      card.appendChild(el("p", null, f.why));
      card.appendChild(el("p", "fix", f.fix));
      section.appendChild(card);
    });

    return section;
  }

  function renderBlueprint(result) {
    var section = el("div", "section");
    section.appendChild(el("h2", null, "Recommended shape"));
    section.appendChild(el("p", "muted", result.blueprint.reason));

    section.appendChild(el("h3", null, "Config block"));
    var pre = el("pre", "code", result.blueprint.config_block);
    section.appendChild(pre);

    if (result.blueprint.tests.length) {
      section.appendChild(el("h3", null, "Tests to add"));
      var list = el("ul");
      result.blueprint.tests.forEach(function (test) {
        list.appendChild(el("li", null, test.column + ": " + test.tests.join(", ") + " - " + test.reason));
      });
      section.appendChild(list);
    }

    section.appendChild(el("h3", null, "schema.yml"));
    section.appendChild(el("pre", "code", result.blueprint.schema_yml));

    section.appendChild(el("h3", null, "Shape checklist"));
    var checklist = el("ul");
    result.blueprint.structure.forEach(function (step) {
      checklist.appendChild(el("li", null, step));
    });
    section.appendChild(checklist);
    return section;
  }

  function renderPlan(result) {
    var section = el("div", "section");
    section.appendChild(el("h2", null, "Fix plan"));
    var list = el("ol", "plan");
    result.score.fix_plan.forEach(function (step) {
      var item = el("li");
      item.appendChild(el("span", null, "[" + step.severity + "] " + step.title));
      item.appendChild(el("div", "fixtext", step.fix));
      var gain = step.estimated_gain
        ? "estimated score change +" + step.estimated_gain + " on " + step.axis_label
        : "no score change on " + step.axis_label;
      item.appendChild(el("div", "gain", gain));
      if (step.blocked) item.appendChild(el("div", "blocked", step.blocked));
      list.appendChild(item);
    });
    section.appendChild(list);
    return section;
  }

  /* -------------------------------------------------------------- history */

  function loadRuns() {
    return api("/api/runs").then(function (rows) {
      var holder = document.getElementById("runs");
      clear(holder);
      if (!rows.length) {
        holder.appendChild(el("p", "muted", "No saved reviews yet."));
        return;
      }
      var table = el("table", "runs");
      var head = el("tr");
      ["When", "Model", "Verdict", "Score", "Findings", "Rating", "Notes", "Export"].forEach(function (title) {
        head.appendChild(el("th", null, title));
      });
      table.appendChild(head);
      rows.forEach(function (row) {
        var tr = el("tr");
        tr.appendChild(el("td", "muted", row.created_at.replace("T", " ").replace("+00:00", "Z")));
        tr.appendChild(el("td", null, row.model));
        tr.appendChild(el("td", null, row.verdict.replace(/_/g, " ")));
        tr.appendChild(el("td", null, row.overall));
        tr.appendChild(el("td", null, row.finding_count));

        var stars = el("td");
        var starBox = el("div", "stars");
        for (var i = 1; i <= 5; i += 1) {
          (function (value) {
            var star = el("span", "star" + (row.rating && row.rating >= value ? " on" : ""), "★");
            star.addEventListener("click", function () {
              api("/api/runs/" + row.id, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rating: value })
              }).then(loadRuns);
            });
            starBox.appendChild(star);
          })(i);
        }
        stars.appendChild(starBox);
        tr.appendChild(stars);

        var notes = el("td");
        var input = el("input");
        input.type = "text";
        input.value = row.notes || "";
        input.placeholder = "what did you change?";
        input.addEventListener("change", function () {
          api("/api/runs/" + row.id, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ notes: input.value })
          });
        });
        notes.appendChild(input);
        tr.appendChild(notes);

        var links = el("td");
        var md = el("a", null, "md");
        md.href = "/api/export/" + row.id + "?fmt=md";
        md.target = "_blank";
        links.appendChild(md);
        links.appendChild(document.createTextNode(" "));
        var jsLink = el("a", null, "json");
        jsLink.href = "/api/export/" + row.id + "?fmt=json";
        jsLink.target = "_blank";
        links.appendChild(jsLink);
        tr.appendChild(links);

        table.appendChild(tr);
      });
      holder.appendChild(table);
    }).catch(function (err) { status(err.message, true); });
  }

  /* ----------------------------------------------------------------- init */

  document.addEventListener("DOMContentLoaded", function () {
    loadProfiles();
    loadSampleList();
    loadRuns();
    document.getElementById("run").addEventListener("click", run);
    document.getElementById("sample-select").addEventListener("change", function (event) {
      loadSample(event.target.value);
    });
  });
})();
