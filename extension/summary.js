// The same summary as connector/har.py. tests/test_extension.py keeps the two the same.
"use strict";

(() => {
  const ID = /^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|[0-9a-fA-F]{16,})$/;
  const API_TYPES = ["json", "graphql", "xml", "text/plain"];
  const MAX_KEYS = 40;
  const PASSWORD_KEY = /(password|passwd|passcode|pwd)$/i;

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

  function template(path) {
    return path.split("/").map((part) => {
      const clean = part.split(";")[0];
      return ID.test(clean) ? "{id}" : clean;
    }).join("/");
  }

  function typeName(value) {
    if (value === null) return "NoneType";
    if (typeof value === "string") return "str";
    if (typeof value === "boolean") return "bool";
    return Number.isInteger(value) ? "int" : "float";
  }

  function shape(value, depth = 5) {
    if (depth === 0) return "...";
    if (isObject(value)) {
      const keys = Object.keys(value);
      if (keys.length && keys.every((key) => ID.test(key))) {
        return { "{id}": shape(value[keys[0]], depth - 1) };
      }
      return Object.fromEntries(keys.slice(0, MAX_KEYS).map((key) => [key, shape(value[key], depth - 1)]));
    }
    if (Array.isArray(value)) return value.length ? [shape(value[0], depth - 1)] : [];
    return typeName(value);
  }

  function parseJson(text) {
    try {
      return text ? JSON.parse(text) : null;
    } catch {
      return null;
    }
  }

  function filled(value) {
    if (Array.isArray(value)) return value.length > 0;
    return isObject(value) ? Object.keys(value).length > 0 : Boolean(value);
  }

  function parseForm(text) {
    const form = {};
    for (const pair of text.split("&")) {
      const split = pair.indexOf("=");
      if (split < 0 || split === pair.length - 1) continue;
      try {
        form[decodeURIComponent(pair.slice(0, split).replace(/\+/g, " "))] = "str";
      } catch {
        form[pair.slice(0, split)] = "str";
      }
    }
    return form;
  }

  function requestBody(text) {
    const parsed = parseJson(text);
    if (filled(parsed)) return parsed;
    const form = parseForm(text || "");
    return filled(form) ? form : null;
  }

  function hash(item) {
    const persisted = isObject(item) && isObject(item.extensions) ? item.extensions.persistedQuery : null;
    return isObject(persisted) ? persisted.sha256Hash || "" : "";
  }

  function operation(entry, body) {
    const names = (entry.request.queryString || []).filter((q) => q.name === "operationName").map((q) => q.value);
    for (const item of Array.isArray(body) ? body : [body]) {
      const name = (isObject(item) && item.operationName) || hash(item).slice(0, 12);
      if (name) names.push(name);
    }
    return names.join(",") || null;
  }

  function responseJson(content) {
    let text = content.text;
    if (text && content.encoding === "base64") {
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(atob(text), (c) => c.charCodeAt(0)));
      } catch {
        return null;
      }
    }
    return parseJson(text);
  }

  function kind(entry) {
    const mime = (entry.response.content.mimeType || "").toLowerCase();
    if (entry.request.method !== "GET" || API_TYPES.some((type) => mime.includes(type))) return "api";
    return mime.includes("html") ? "document" : "static";
  }

  function summarize(har, host = "") {
    const groups = new Map();
    const documents = new Map();
    let staticFiles = 0;
    for (const entry of har.log.entries) {
      const url = new URL(entry.request.url);
      if (!url.host.includes(host)) continue;
      const type = kind(entry);
      if (type === "static") {
        staticFiles += 1;
        continue;
      }
      const path = template(url.pathname);
      const query = [...new Set((entry.request.queryString || []).map((q) => q.name))].sort();
      if (type === "document") {
        const key = `${url.host}${path}${query.length ? `?${query.join(",")}` : ""}`;
        documents.set(key, (documents.get(key) || 0) + 1);
        continue;
      }
      const body = requestBody(entry.request.postData?.text);
      const name = operation(entry, body);
      const key = JSON.stringify([entry.request.method, url.host, path, name]);
      if (!groups.has(key)) {
        groups.set(key, {
          method: entry.request.method, host: url.host, path, operation: name, hash: hash(body) || null,
          count: 0, statuses: {}, query: new Set(), pages: new Set(), request: null, response: null,
        });
      }
      const group = groups.get(key);
      group.count += 1;
      const status = entry.response.status;
      group.statuses[status] = (group.statuses[status] || 0) + 1;
      query.forEach((item) => group.query.add(item));
      if (entry.page) group.pages.add(entry.page);
      if (group.request === null && body !== null) group.request = shape(body);
      const answer = group.response === null ? responseJson(entry.response.content) : null;
      if (answer !== null) group.response = shape(answer);
    }
    const api = [...groups.values()].map((group) => ({
      ...group, query: [...group.query].sort(), pages: [...group.pages].sort(),
    }));
    const sorted = [...documents].sort((a, b) => b[1] - a[1]);
    return { api, documents: Object.fromEntries(sorted), static: staticFiles };
  }

  const emptySummary = () => ({ api: [], documents: {}, static: 0 });

  const union = (left, right) => [...new Set([...left, ...right])].sort();

  function merge(total, part) {
    for (const group of part.api) {
      const same = total.api.find((other) => ["method", "host", "path", "operation"]
        .every((key) => other[key] === group[key]));
      if (!same) {
        total.api.push(group);
        continue;
      }
      same.count += group.count;
      for (const [status, count] of Object.entries(group.statuses)) {
        same.statuses[status] = (same.statuses[status] || 0) + count;
      }
      same.query = union(same.query, group.query);
      same.pages = union(same.pages, group.pages);
      same.request ??= group.request;
      same.response ??= group.response;
    }
    for (const [path, count] of Object.entries(part.documents)) {
      total.documents[path] = (total.documents[path] || 0) + count;
    }
    total.static += part.static;
    return total;
  }

  function bodyText(body) {
    if (body?.formData) {
      const pairs = Object.entries(body.formData).flatMap(([key, values]) => values.map((value) => [key, value]));
      return new URLSearchParams(pairs).toString();
    }
    if (!body?.raw) return undefined;
    const decoder = new TextDecoder();
    return body.raw.filter((part) => part.bytes).map((part) => decoder.decode(part.bytes, { stream: true })).join("");
  }

  function maskValue(value) {
    if (Array.isArray(value)) return value.map(maskValue);
    if (!isObject(value)) return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => (
      [key, PASSWORD_KEY.test(key) ? "***" : maskValue(item)])));
  }

  function maskPassword(text) {
    if (!text) return text;
    try {
      return JSON.stringify(maskValue(JSON.parse(text)));
    } catch {
      const form = new URLSearchParams(text);
      for (const key of [...form.keys()]) if (PASSWORD_KEY.test(key)) form.set(key, "***");
      return text.includes("=") ? form.toString() : text;
    }
  }

  const hostOf = (hostname) => hostname.replace(/^www\./, "");

  function patternFor(hostname) {
    const host = hostOf(hostname);
    const plain = /^[\d.]+$/.test(host) || !host.includes(".");
    return plain ? `*://${host}/*` : `*://*.${host}/*`;
  }

  globalThis.connectorSummary = {
    API_TYPES, bodyText, emptySummary, hostOf, maskPassword, merge, patternFor, summarize, template,
  };
})();
