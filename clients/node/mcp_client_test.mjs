// Contract test for spec §43: a JavaScript/Node MCP client (what ZTech's Electron MAIN process will use)
// spawns the Python MCP server over stdio and consumes the canonical JSON report.
//
//   cd clients/node && npm install && PYTHON=python3 npm test
//
// Uses an unresolvable domain so the test is deterministic with or without internet:
// the engine must answer honestly (status "failed", COMPANY_NOT_FOUND) instead of fabricating data.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "..", "..", "src");
const dbDir = mkdtempSync(join(tmpdir(), "ztech-oi-node-"));

const transport = new StdioClientTransport({
  command: process.env.PYTHON || "python3",
  args: ["-m", "ztech_oi.adapters.mcp_server"],
  env: {
    ...process.env,
    PYTHONPATH: src,
    ZTECH_OI_DB_PATH: join(dbDir, "oi.sqlite3"),
    META_ACCESS_TOKEN: "NODE_TEST_SECRET",
  },
  stderr: "pipe",
});
const client = new Client({ name: "ztech-node-contract-test", version: "1.0.0" });
await client.connect(transport);

const results = [];
const check = async (name, fn) => {
  await fn();
  results.push(name);
};

await check("tools/list exposes exactly the 8 research tools", async () => {
  const { tools } = await client.listTools();
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    [
      "analyze_opportunity", "analyze_prospect_opportunity", "discover_competitors", "get_engine_info",
      "get_opportunity_report", "get_opportunity_timeline", "research_company", "research_competitors",
    ],
  );
});

await check("get_engine_info returns structured JSON without secrets", async () => {
  const res = await client.callTool({ name: "get_engine_info", arguments: {} });
  assert.ok(!res.isError, res.content?.[0]?.text);
  assert.equal(res.structuredContent.schema_version, "1.0");
  assert.equal(res.structuredContent.configuration.meta_ad_library.configured, true);
  assert.ok(!JSON.stringify(res).includes("NODE_TEST_SECRET"), "secret leaked to client");
});

let researchId;
await check("analyze_prospect_opportunity is honest when nothing can be observed", async () => {
  const res = await client.callTool({
    name: "analyze_prospect_opportunity",
    arguments: {
      prospect: { company_name: "Nowhere Bakery", domain: "nowhere-bakery-zt-test.invalid-tld-example.com" },
      options: { max_competitors: 0, idempotency_key: "node-contract-1" },
    },
  });
  assert.ok(!res.isError, res.content?.[0]?.text);
  const s = res.structuredContent;
  assert.equal(s.schema_version, "1.0");
  assert.equal(s.status, "failed");
  assert.ok(s.limitations[0].startsWith("COMPANY_NOT_FOUND"));
  assert.deepEqual(s.opportunities, []);
  researchId = s.research_id;
});

await check("get_opportunity_report returns the canonical report (section filter)", async () => {
  const res = await client.callTool({
    name: "get_opportunity_report",
    arguments: { research_id: researchId, sections: ["provider_status", "limitations"] },
  });
  const r = res.structuredContent;
  assert.equal(r.research_id, researchId);
  assert.deepEqual(Object.keys(r).sort(), [
    "generated_at", "limitations", "previous_snapshot_id", "provider_status", "research_id", "schema_version",
    "snapshot_id", "status",
  ]);
  const statuses = Object.values(r.provider_status)[0];
  assert.equal(statuses.linkedin, "unsupported");
});

await check("idempotency_key returns the same research", async () => {
  const res = await client.callTool({
    name: "analyze_prospect_opportunity",
    arguments: {
      prospect: { company_name: "Nowhere Bakery", domain: "nowhere-bakery-zt-test.invalid-tld-example.com" },
      options: { max_competitors: 0, idempotency_key: "node-contract-1" },
    },
  });
  assert.equal(res.structuredContent.research_id, researchId);
});

await check("errors come back as isError with a stable code", async () => {
  const nf = await client.callTool({ name: "get_opportunity_report", arguments: { research_id: "res_missing_x" } });
  assert.equal(nf.isError, true);
  assert.match(nf.content[0].text, /"NOT_FOUND"/);
  const bad = await client.callTool({
    name: "analyze_prospect_opportunity",
    arguments: { prospect: { company_name: "x", domain: "localhost" } },
  });
  assert.equal(bad.isError, true);
  assert.match(bad.content[0].text, /INVALID_DOMAIN/);
});

await client.close();
console.log(JSON.stringify({ ok: true, passed: results }));
