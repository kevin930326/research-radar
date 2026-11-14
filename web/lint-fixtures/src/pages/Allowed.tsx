import { datasource } from "../datasource";
import type { SiteKind } from "../datasource/types";

export function Allowed(): SiteKind {
  return datasource.siteKind();
}
