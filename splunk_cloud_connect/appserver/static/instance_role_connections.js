// Cloud Connect: set up instance-role connections (Bedrock models and
// knowledge bases with a role ARN and no access keys). A thin client over the
// admin-only endpoint /services/aitk_local/connections; all checks happen there.
require(["jquery", "splunkjs/mvc", "splunkjs/mvc/simplexml/ready!"], function ($, mvc) {
    "use strict";
    var BASE = "/services/aitk_local/connections";
    var service = mvc.createService({ owner: "nobody", app: "splunk_cloud_connect" });
    var root = document.getElementById("irc-app");

    var KINDS = {
        models: {
            title: "Bedrock models",
            fields: [
                { key: "name", label: "Name", hint: "Letters, digits and underscores. Saved with an _instance_role suffix." },
                { key: "region", label: "Region", placeholder: "us-east-1" },
                { key: "role_arn", label: "Role ARN", placeholder: "arn:aws:iam::123456789012:role/bedrock-invoke",
                  hint: "The role in the Bedrock account. Its trust policy must name this search head's instance role." },
                { key: "model", label: "Model ID", list: true, hint: "Use List models, or type the model or inference profile ID." },
                { key: "temperature", label: "Temperature", type: "number", step: "0.1", value: "0" },
                { key: "max_tokens", label: "Max tokens", type: "number", value: "4000" },
                { key: "request_timeout", label: "Request timeout (s)", type: "number", value: "60" },
                { key: "description", label: "Description", textarea: true }
            ],
            columns: [["name", "Name"], ["region", "Region"], ["role_arn", "Role ARN"], ["model", "Model"]]
        },
        knowledge_bases: {
            title: "Knowledge bases",
            fields: [
                { key: "name", label: "Name", hint: "Letters, digits and underscores. Saved with an _instance_role suffix." },
                { key: "region", label: "Region", placeholder: "us-east-1" },
                { key: "role_arn", label: "Role ARN", placeholder: "arn:aws:iam::123456789012:role/kb-search",
                  hint: "Needs bedrock:Retrieve on this knowledge base." },
                { key: "kb_id", label: "Knowledge base ID", placeholder: "EJGJR4SKJL" },
                { key: "description", label: "Description", textarea: true,
                  hint: "All the agent learns about this knowledge base: say what's in it and when to search it." }
            ],
            columns: [["name", "Name"], ["region", "Region"], ["role_arn", "Role ARN"], ["kb_id", "Knowledge base ID"]]
        }
    };

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        Object.keys(attrs || {}).forEach(function (k) {
            if (k === "text") { node.textContent = attrs[k]; } else if (k === "onclick") { node.onclick = attrs[k]; }
            else { node.setAttribute(k, attrs[k]); }
        });
        (children || []).forEach(function (c) { if (c) { node.appendChild(c); } });
        return node;
    }

    function call(method, path, body) {
        var done = $.Deferred();
        service.request(BASE + path, method, { output_mode: "json" }, null,
            body === undefined ? null : JSON.stringify(body), { "Content-Type": "application/json" },
            function (err, response) {
                var status = err ? err.status : response.status;
                var data = err ? err.data : response.data;
                if (typeof data === "string") { try { data = JSON.parse(data); } catch (e) { data = { message: data }; } }
                done.resolve(status, data || {});
            });
        return done.promise();
    }

    function encode(name) { return "/" + encodeURIComponent(name); }

    function render(listing) {
        root.textContent = "";
        root.appendChild(el("p", { "class": "irc-note", text:
            "These connections have no access keys. The search head's instance profile assumes each connection's " +
            "role. Edit them only here: AI Toolkit's form asks for keys, and its Test connection button always " +
            "fails for them." }));
        Object.keys(KINDS).forEach(function (kind) { section(kind, listing[kind] || []); });
    }

    function section(kind, rows) {
        var spec = KINDS[kind];
        root.appendChild(el("h2", { text: spec.title }));
        var holder = el("div");
        if (!rows.length) {
            root.appendChild(el("p", { "class": "irc-note", text: "None yet." }));
        } else {
            var head = el("tr", {}, spec.columns.map(function (c) { return el("th", { text: c[1] }); }).concat([el("th")]));
            var body = rows.map(function (row) {
                var cells = spec.columns.map(function (c) {
                    return el("td", { text: row[c[0]] || "", "class": c[0] === "role_arn" ? "irc-arn" : "" });
                });
                cells.push(el("td", {}, [
                    el("button", { type: "button", "class": "btn", text: "Edit", onclick: function () { form(holder, kind, row); } }),
                    el("button", { type: "button", "class": "btn", text: "Delete", onclick: function () { remove(kind, row.name); } })
                ]));
                return el("tr", {}, cells);
            });
            root.appendChild(el("table", {}, [el("thead", {}, [head]), el("tbody", {}, body)]));
        }
        root.appendChild(el("button", { type: "button", "class": "btn btn-primary", text: "New " + (kind === "models" ? "model" : "knowledge base"),
                                        onclick: function () { form(holder, kind, null); } }));
        root.appendChild(holder);
    }

    function form(holder, kind, existing) {
        var spec = KINDS[kind];
        holder.textContent = "";
        var inputs = {};
        var children = [];
        spec.fields.forEach(function (f) {
            var attrs = { id: "irc-" + kind + "-" + f.key };
            if (f.placeholder) { attrs.placeholder = f.placeholder; }
            if (f.type) { attrs.type = f.type; }
            if (f.step) { attrs.step = f.step; }
            if (f.list) { attrs.list = "irc-models-list"; }
            var input = el(f.textarea ? "textarea" : "input", attrs);
            var value = existing ? existing[f.key] : f.value;
            input.value = value === undefined || value === null ? "" : value;
            if (existing && f.key === "name") { input.readOnly = true; }
            inputs[f.key] = input;
            children.push(el("label", { "for": attrs.id, text: f.label }), input);
            if (f.hint) { children.push(el("div", { "class": "irc-hint", text: f.hint })); }
        });
        var datalist = el("datalist", { id: "irc-models-list" });
        var result = el("div");
        function values() {
            var out = {};
            Object.keys(inputs).forEach(function (k) { out[k] = inputs[k].value.trim(); });
            return out;
        }
        function show(ok, message) {
            result.className = "irc-result " + (ok ? "irc-ok" : "irc-fail");
            result.textContent = message;
        }
        function save(anyway) {
            var body = values();
            if (anyway) { body.save_anyway = true; }
            var path = "/" + kind + (existing ? encode(existing.name) : "");
            show(true, "Testing the connection…");
            call("POST", path, body).done(function (status, data) {
                if (status === 200 || status === 201) { load(); return; }
                show(false, data.message || ("Error " + status));
                if (status === 422) {
                    result.appendChild(el("div", {}, [el("button", { type: "button", "class": "btn", text: "Save anyway",
                        onclick: function () { save(true); } })]));
                }
            });
        }
        var buttons = [
            el("button", { type: "button", "class": "btn", text: "Test", onclick: function () {
                show(true, "Testing…");
                call("POST", "/" + kind + "/test", values()).done(function (status, data) {
                    var test = data.test || {};
                    show(status === 200 && test.ok, test.message || data.message || ("Error " + status));
                });
            } }),
            el("button", { type: "button", "class": "btn btn-primary", text: existing ? "Save changes" : "Create",
                           onclick: function () { save(false); } }),
            el("button", { type: "button", "class": "btn", text: "Cancel", onclick: function () { holder.textContent = ""; } })
        ];
        if (kind === "models") {
            buttons.unshift(el("button", { type: "button", "class": "btn", text: "List models", onclick: function () {
                var v = values();
                show(true, "Listing models as the role…");
                call("POST", "/models/available", { region: v.region, role_arn: v.role_arn }).done(function (status, data) {
                    datalist.textContent = "";
                    (data.models || []).forEach(function (m) { datalist.appendChild(el("option", { value: m.id, label: m.name })); });
                    show(status === 200 && !data.message, data.message ||
                        ((data.models || []).length + " models found. Pick one in Model ID."));
                });
            } }));
        }
        children.push(el("div", { "class": "irc-buttons" }, buttons), result, datalist);
        holder.appendChild(el("form", {}, children));
    }

    function remove(kind, name) {
        if (!window.confirm("Delete " + name + "? Agents using it will stop working.")) { return; }
        call("DELETE", "/" + kind + encode(name)).done(function (status, data) {
            if (status !== 200) { window.alert(data.message || ("Error " + status)); }
            load();
        });
    }

    function load() {
        call("GET", "").done(function (status, data) {
            if (status !== 200) {
                root.textContent = data.message || ("Couldn't load connections (error " + status + ").");
                return;
            }
            render(data);
        });
    }

    load();
});
