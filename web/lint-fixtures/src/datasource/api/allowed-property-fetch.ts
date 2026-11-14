export async function getPapers() {
  const response = await globalThis.fetch("/api/v1/papers");
  return response.json();
}
