export async function ConcatImport(kind: string) {
  const { datasource } = await import("../../datasource/" + kind);
  return datasource.siteKind();
}
