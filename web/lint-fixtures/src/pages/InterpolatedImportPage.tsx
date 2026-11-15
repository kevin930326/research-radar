export async function InterpolatedImportPage(kind: string) {
  const { datasource } = await import(`../datasource/${kind}`);
  return datasource.siteKind();
}
