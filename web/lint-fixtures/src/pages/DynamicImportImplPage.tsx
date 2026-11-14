export async function DynamicImportImplPage() {
  const { datasource } = await import("#datasource-impl");
  return datasource.siteKind();
}
