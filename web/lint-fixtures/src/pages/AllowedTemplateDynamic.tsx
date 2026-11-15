export async function AllowedTemplateDynamic() {
  const { datasource } = await import(`../datasource`);
  return datasource.siteKind();
}
