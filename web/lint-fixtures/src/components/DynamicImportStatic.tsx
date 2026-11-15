export async function DynamicImportStatic() {
  const { datasource } = await import(`../../datasource/static/index`);
  return datasource.siteKind();
}
