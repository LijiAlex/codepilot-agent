// tests/js_smoke.js — run every dashboard page's render code with a tiny fake DOM; print errors as JSON.
// Usage: node tests/js_smoke.js APP_JS RESPONSES_JSON   (responses: {"overview": {...}, "matrix": {...}, ...})
const fs = require("fs"), vm = require("vm");
const [appJs, respFile] = process.argv.slice(2);
const responses = JSON.parse(fs.readFileSync(respFile, "utf8"));
class Node {}
const STRING_PROPS = new Set(["value", "id", "title", "href", "className", "innerText", "name", "placeholder", "type", "rel", "target"]);
function node(tag) {
  const base = Object.assign(new Node(), { tagName: tag, children: [], style: {}, dataset: {}, _text: "",
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } });
  const methods = {
    appendChild(c) { this.children.push(c); return c; }, append(...cs) { this.children.push(...cs); },
    prepend(...cs) { this.children.unshift(...cs); }, insertBefore(c) { this.children.unshift(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }, replaceChildren(...cs) { this.children = cs; },
    querySelectorAll(sel) { const out = []; const walk = n => (n.children || []).forEach(c => { if (c.tagName === sel) out.push(c); walk(c); }); walk(this); return out; },
    querySelector(sel) { return methods.querySelectorAll.call(this, sel)[0] || null; }, closest() { return null; }, contains() { return false; },
  };
  return new Proxy(base, {
    get(t, p) {
      if (p === "textContent") return t._text;
      if (p === "firstChild") return t.children[0] || null;
      if (p === "lastChild") return t.children[t.children.length - 1] || null;
      if (p in methods) return methods[p].bind(t);
      if (p in t) return t[p];
      if (typeof p === "string" && STRING_PROPS.has(p)) return "";
      if (p === "checked" || p === "disabled" || p === "open") return false;
      return () => undefined;                       // any other DOM method (focus, remove, addEventListener, …): harmless no-op
    },
    set(t, p, v) { if (p === "textContent") t._text = String(v); else t[p] = v; return true; },
  });
}
const doc = { createElement: node, createTextNode: t => Object.assign(new Node(), { textContent: String(t) }), getElementById: () => node("div"),
  querySelectorAll: () => [], addEventListener() {}, body: node("body") };
const ctx = { document: doc, Node, console, JSON, Math, Number, String, Object, Array, Set, Map, Date, Promise, URLSearchParams,
  RegExp, Error, encodeURIComponent, isNaN, parseInt, parseFloat, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  sessionStorage: { getItem: () => "t", setItem() {} }, localStorage: { getItem: () => null, setItem() {} },
  location: { hash: "", href: "" }, history: { replaceState() {} }, confirm: () => false, alert() {},
  window: { addEventListener() {} },
  fetch: async (url) => { const page = url.replace(/^\/api\//, "").split("?")[0];
    const body = page.startsWith("runs/") ? responses["run"] : responses[page];
    return { ok: true, status: 200, json: async () => body || {} }; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appJs, "utf8") + "\nthis.__render = render; this.__showRun = showRun;", ctx);
(async () => {
  const errors = {};
  for (const page of Object.keys(ctx.__render)) {
    try { await ctx.__render[page](node("main")); } catch (e) { errors[page] = String(e && e.stack || e).split("\n").slice(0, 3).join(" | "); }
  }
  try { await ctx.__showRun("x"); } catch (e) { errors.run = String(e); }
  console.log(JSON.stringify(errors));
})();
