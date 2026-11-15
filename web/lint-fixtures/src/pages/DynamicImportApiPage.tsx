export async function DynamicImportApiPage() {
  const { datasource } = await import("../datasource/api");
  return datasource.siteKind();
}
