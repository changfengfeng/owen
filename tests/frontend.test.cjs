const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

async function setup() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) {
      nodes.set(id, {
        hidden: false,
        value: "previous account data",
        textContent: "previous account data",
        classList: { toggle() {} },
        replaceChildren() { this.textContent = ""; },
        removeAttribute(name) { delete this[name]; },
        setAttribute() {},
        append() {},
      });
    }
    return nodes.get(id);
  }
  const context = vm.createContext({
    document: { getElementById: node, createElement: () => node(Symbol()), createTextNode: (x) => x },
    fetch: async () => ({ status: 401, ok: false, json: async () => ({ detail: "signed out" }) }),
    clearInterval() {},
    setInterval() {},
    Intl, Date, Error,
  });
  vm.runInContext(readFileSync(path.join(__dirname, "../src/cloud_agent/static/app.js"), "utf8"), context);
  await new Promise(setImmediate);
  return { context, node, run: (code) => vm.runInContext(code, context) };
}

test("signing out clears private task state and closes the event connection", async () => {
  const { context, node, run } = await setup();
  let closed = false;
  context.connection = { close() { closed = true; } };
  run("actor={email:'first@example.com'}; selected='task-1'; tasks=[{id:'task-1'}]; source=connection;");
  node("summary").textContent = "private code";
  node("download-patch").href = "/api/tasks/task-1/patch";
  run("showAuth()");
  assert.ok(closed);
  assert.equal(run("actor"), null);
  assert.equal(run("tasks.length"), 0);
  assert.equal(node("task-detail").hidden, true);
  assert.equal(node("summary").textContent, "");
  assert.equal(node("download-patch").href, undefined);
  assert.equal(node("prompt").value, "");
});

test("a late response from the previous account cannot render its tasks", async () => {
  const { context, run } = await setup();
  const resolve = [];
  context.fetch = () => new Promise((done) => resolve.push(done));
  run("actor={email:'first@example.com'}");
  const refreshing = run("refresh()");
  run("showAuth(); actor={email:'second@example.com'}");
  resolve[0]({ status: 200, ok: true, json: async () => ({ tasks: [{ id: "private-first" }] }) });
  resolve[1]({ status: 200, ok: true, json: async () => ({}) });
  await refreshing;
  assert.equal(run("tasks.length"), 0);
});

test("a late unauthorized response cannot sign out a new account", async () => {
  const { context, run } = await setup();
  let resolve;
  context.fetch = () => new Promise((done) => { resolve = done; });
  run("actor={email:'first@example.com'}");
  const request = run("api('/tasks')");
  run("actor={email:'second@example.com'}");
  resolve({ status: 401, ok: false, json: async () => ({ detail: "expired" }) });
  await assert.rejects(request);
  assert.equal(run("actor.email"), "second@example.com");
});
