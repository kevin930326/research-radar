export async function AliasedFetch() {
  const browser = globalThis;
  const response = await browser.fetch("/api/v1/papers");
  return response.ok;
}
