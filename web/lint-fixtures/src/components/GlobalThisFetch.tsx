export async function GlobalThisFetch() {
  const response = await globalThis.fetch("/api/v1/papers");
  return response.ok;
}
