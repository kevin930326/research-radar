export async function AllowedDynamic() {
  const { datasource } = await import("../datasource");
  return datasource.siteKind();
}
