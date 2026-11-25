// 示範站讀 preview/（Story 3.14 示範站）：用 repo 裡的範例匯出（e2e/fixtures/demo-preview，假論文）讀 manifest 與
// 它指到的三個檔；檔案的形狀不對、和 manifest 不是同一次匯出、讀不到時丟出例外；DOI 連結和後端寫進分片的一樣。
// 標題比對（AD-20）：NFKC 正規化、不分大小寫、字面包含。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { doiUrl, loadDemoManifest, loadDemoPaperIndex, PreviewFileError } from "./static/preview";
import { titleMatches } from "./title-match";

const FIXTURE = fileURLToPath(new URL("../../e2e/fixtures/demo-preview/", import.meta.url));

type Json = Record<string, unknown>;

function readFixture(path: string): Json {
  return JSON.parse(readFileSync(`${FIXTURE}${path}`, "utf8")) as Json;
}

/** 假的 fetch：`/preview/<path>` 回範例檔；`change` 可以在回傳前改掉某個檔的內容。 */
function fixtureFetch(change: (path: string, body: Json) => Json | number = (_path, body) => body): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const url = String(input);
    expect(url.startsWith("/preview/")).toBe(true);
    const path = url.slice("/preview/".length);
    const result = change(path, readFixture(path));
    if (typeof result === "number") return new Response(null, { status: result });
    return new Response(JSON.stringify(result), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
}

async function load(fetchImpl: typeof fetch) {
  const manifest = await loadDemoManifest(fetchImpl);
  return loadDemoPaperIndex(manifest, fetchImpl);
}

describe("demo preview loader", () => {
  it("reads the manifest, then the taxonomy, index and titles of the same export", async () => {
    const manifest = await loadDemoManifest(fixtureFetch());
    expect(manifest).toMatchObject({ exportId: "2-1", pageSize: 50, snapshot: { number: 2, as_of: "2026-11-16T03:00:00Z" } });
    const index = await loadDemoPaperIndex(manifest, fixtureFetch());
    expect(index.rows).toHaveLength(60);
    expect(index.names.journals.map((journal) => journal.key)).toContain("misq");
    const titles = readFixture("2-1/titles.json").titles as string[];
    expect(index.rows.map((row) => row.title)).toEqual(titles);
    // 分類的來歷換成來歷本身；摘要來源不允許公開的那一篇沒有分類狀態。
    expect(index.rows.filter((row) => row.classificationState === null)).toHaveLength(1);
    expect(index.rows.find((row) => row.classificationState === "ready")?.classificationProvenance).toMatchObject({ model: "claude-haiku-5-5" });
  });

  it("builds the same DOI link the backend writes into the paper shards", async () => {
    const index = await load(fixtureFetch());
    const shard = readFixture("2-1/papers/000.json").papers as { biblio: { biblio: { id: string; doi_url: string | null } } }[];
    expect(shard.length).toBeGreaterThan(0);
    for (const paper of shard) {
      const row = index.rows.find((candidate) => candidate.id === paper.biblio.biblio.id);
      expect(row?.doiUrl).toBe(paper.biblio.biblio.doi_url);
    }
  });

  it("encodes DOI characters like Python quote(doi, safe='/')", () => {
    expect(doiUrl("10.1000/abc(1)def")).toBe("https://doi.org/10.1000/abc%281%29def");
    expect(doiUrl("10.1000/a b<c>")).toBe("https://doi.org/10.1000/a%20b%3Cc%3E");
    expect(doiUrl("10.1000/x;y!z*'")).toBe("https://doi.org/10.1000/x%3By%21z%2A%27");
    expect(doiUrl("10.1000/A-Z_a.z~0")).toBe("https://doi.org/10.1000/A-Z_a.z~0");
  });

  it("rejects a file from a different export or snapshot", async () => {
    await expect(load(fixtureFetch((path, body) => (path.endsWith("titles.json") ? { ...body, export_id: "2-2" } : body)))).rejects.toThrow(
      PreviewFileError,
    );
    await expect(
      load(fixtureFetch((path, body) => (path.endsWith("taxonomy.json") ? { ...body, snapshot: { number: 3, as_of: "2026-11-16T03:00:00Z" } } : body))),
    ).rejects.toThrow(PreviewFileError);
  });

  it("rejects columns of different lengths and values of the wrong type", async () => {
    const shorter = (field: string) =>
      fixtureFetch((path, body) => (path.endsWith("papers-index.json") ? { ...body, [field]: (body[field] as unknown[]).slice(1) } : body));
    await expect(load(shorter("year"))).rejects.toThrow(PreviewFileError);
    await expect(load(shorter("topics_verified"))).rejects.toThrow(PreviewFileError);
    const changed = (field: string, value: unknown) =>
      fixtureFetch((path, body) => {
        if (!path.endsWith("papers-index.json")) return body;
        const column = [...(body[field] as unknown[])];
        column[0] = value;
        return { ...body, [field]: column };
      });
    await expect(load(changed("has_abstract", "yes"))).rejects.toThrow(PreviewFileError);
    await expect(load(changed("journal", "../misq"))).rejects.toThrow(PreviewFileError);
    await expect(load(changed("classification_state", "done"))).rejects.toThrow(PreviewFileError);
    await expect(load(changed("classification_provenance", 9999))).rejects.toThrow(PreviewFileError);
    await expect(load(changed("id", "not-a-uuid"))).rejects.toThrow(PreviewFileError);
  });

  it("rejects a manifest that does not list the files it needs, or has no page size", async () => {
    await expect(
      load(fixtureFetch((path, body) => (path === "manifest.json" ? { ...body, files: (body.files as Json[]).filter((file) => file.path !== "2-1/titles.json") } : body))),
    ).rejects.toThrow(PreviewFileError);
    await expect(loadDemoManifest(fixtureFetch((path, body) => (path === "manifest.json" ? { ...body, client_config: {} } : body)))).rejects.toThrow(
      PreviewFileError,
    );
    await expect(loadDemoManifest(fixtureFetch((path, body) => (path === "manifest.json" ? { ...body, export_id: "../2-1" } : body)))).rejects.toThrow(
      PreviewFileError,
    );
  });

  it("fails when a file cannot be read", async () => {
    await expect(loadDemoManifest(fixtureFetch(() => 404))).rejects.toThrow("HTTP 404");
    await expect(load(fixtureFetch((path, body) => (path.endsWith("papers-index.json") ? 500 : body)))).rejects.toThrow("HTTP 500");
  });
});

/** 半形英文字母換成全形（字碼產生，程式裡不寫死全形字：lint 把它算成中文）。 */
function fullWidth(text: string): string {
  return [...text].map((char) => String.fromCodePoint(char.codePointAt(0)! + 0xfee0)).join("");
}

describe("title match", () => {
  it("matches a literal substring anywhere, ignoring case and full-width forms", () => {
    expect(titleMatches("Blockchain Governance in Platforms", "chain gov")).toBe(true);
    expect(titleMatches("Blockchain Governance", "BLOCKCHAIN")).toBe(true);
    expect(titleMatches("Blockchain Governance", fullWidth("block"))).toBe(true);
    expect(titleMatches(`${fullWidth("AI")} Adoption`, "ai")).toBe(true);
    expect(titleMatches("Blockchain Governance", "block-chain")).toBe(false);
    expect(titleMatches("Blockchain", "   ")).toBe(true);
  });
});
