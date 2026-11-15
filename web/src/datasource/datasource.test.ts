// 兩種資料來源都滿足同一份 DataSource 型別（型別檢查時就會擋），並回報自己是哪一個站。
import { describe, expect, expectTypeOf, it } from "vitest";

import { API_BASE, datasource as api } from "./api";
import { datasource as entry } from "./index";
import { datasource as staticSource } from "./static";
import type { DataSource } from "./types";

describe("datasource", () => {
  it("both implementations satisfy DataSource", () => {
    expectTypeOf(api).toEqualTypeOf<DataSource>();
    expectTypeOf(staticSource).toEqualTypeOf<DataSource>();
    expectTypeOf(entry).toEqualTypeOf<DataSource>();
  });

  it("each implementation reports its site", () => {
    expect(api.siteKind()).toBe("member");
    expect(staticSource.siteKind()).toBe("demo");
  });

  it("the entry resolves to the member implementation outside a demo build", () => {
    expect(entry.siteKind()).toBe("member");
  });

  it("the API base is the versioned prefix", () => {
    expect(API_BASE).toBe("/api/v1");
  });
});
